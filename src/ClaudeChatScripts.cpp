/*
 * This file is part of the mod-playerbot-claude module.
 */

#include "ClaudeChat.h"

#include <algorithm>

#include "ChannelMgr.h"
#include "Chat.h"
#include "Config.h"
#include "Group.h"
#include "Item.h"
#include "ItemTemplate.h"
#include "Log.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "QuestDef.h"
#include "ScriptMgr.h"
#include "SharedDefines.h"

#include "Bot/Social/PlayerbotSocialMgr.h"
#include "Bot/Social/PlayerbotSocialRoute.h"
#include "ExternalEventHelper.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"

#include <cctype>
#include <unordered_map>

namespace
{
    using namespace ClaudeChat;

    constexpr size_t MAX_CAPTURED_TEXT_BYTES = 512;
    constexpr size_t RECENT_EVENT_CAPACITY = 256;

    // World-thread delivery routing for one in-flight request. Only immutable values are
    // stored; players are re-resolved from GUIDs at delivery time.
    struct PendingDelivery
    {
        ChatChannel channel = ChatChannel::Whisper;
        ObjectGuid botGuid;
        ObjectGuid speakerGuid;
        int64 expiresAtSteadyMs = 0;
    };

    struct PendingCareerDelivery
    {
        PlayerbotCareerPlanRequest request;
        int64 expiresAtSteadyMs = 0;
    };

    bool IsMachineBot(Player* candidate)
    {
        if (!candidate)
            return false;

        PlayerbotAI* botAI = GET_PLAYERBOT_AI(candidate);
        return botAI && !botAI->IsRealPlayer();
    }

    bool IsRealPlayerActor(Player* actor)
    {
        if (!actor)
            return false;

        PlayerbotAI* botAI = GET_PLAYERBOT_AI(actor);
        return !botAI || botAI->IsRealPlayer();
    }

    bool HasOnlineHuman()
    {
        for (auto const& entry : ObjectAccessor::GetPlayers())
        {
            Player* player = entry.second;
            if (player && player->IsInWorld() && IsRealPlayerActor(player))
                return true;
        }

        return false;
    }

    bool HasOnlineHumanOnTeam(TeamId teamId)
    {
        for (auto const& entry : ObjectAccessor::GetPlayers())
        {
            Player* player = entry.second;
            if (player && player->IsInWorld() && player->GetTeamId() == teamId && IsRealPlayerActor(player))
                return true;
        }

        return false;
    }

    bool HasWorldChannel(Player* player)
    {
        if (!player)
            return false;

        ChannelMgr* channelManager = ChannelMgr::forTeam(player->GetTeamId());
        Channel* worldChannel = channelManager ? channelManager->GetChannel("World", player) : nullptr;
        return worldChannel && worldChannel->IsOn(player->GetGUID());
    }

    bool EqualNamesCaseInsensitive(std::string const& a, std::string const& b)
    {
        if (a.size() != b.size())
            return false;

        for (size_t i = 0; i < a.size(); ++i)
            if (std::tolower(static_cast<unsigned char>(a[i])) != std::tolower(static_cast<unsigned char>(b[i])))
                return false;

        return true;
    }

    std::string BoundedText(std::string text)
    {
        return TruncateUtf8Bytes(std::move(text), MAX_CAPTURED_TEXT_BYTES);
    }

    // All state lives on the world thread; every method below must only be called from
    // world-thread hooks. The bridge worker is the only other thread and it touches only
    // the bounded queues inside ClaudeBridge.
    /*
     * Both provider seams on one object, because they share the one bridge and the one lifetime.
     *
     * The career provider and the social provider are unrelated contracts, but a second object would
     * need its own reference to the bridge and its own answer to "has startup run yet", and those two
     * answers drifting apart is how a provider stays registered against a stopped bridge.
     */
    class ClaudeChatState : public PlayerbotCareerPlanProvider, public PlayerbotSocialProvider
    {
    public:
        static ClaudeChatState& Instance()
        {
            static ClaudeChatState instance;
            return instance;
        }

        void Startup()
        {
            if (!sConfigMgr->GetOption<bool>("PlayerbotClaude.Enable", false))
            {
                LOG_INFO("playerbot.claude", "mod-playerbot-claude: disabled by configuration");
                return;
            }

            int32 const port = sConfigMgr->GetOption<int32>("PlayerbotClaude.BridgePort", 0);
            if (port <= 0 || port > 65535)
            {
                LOG_INFO("playerbot.claude", "mod-playerbot-claude: disabled (PlayerbotClaude.BridgePort is not set)");
                return;
            }

            std::optional<std::string> token = BridgeTokenFromEnvironment();
            if (!token)
            {
                LOG_ERROR("playerbot.claude",
                          "mod-playerbot-claude: disabled (PLAYERBOT_CLAUDE_BRIDGE_TOKEN is missing or shorter "
                          "than {} bytes)",
                          MIN_BRIDGE_TOKEN_BYTES);
                return;
            }

            _responseDeadlineMs =
                std::max<int64>(1000, sConfigMgr->GetOption<int32>("PlayerbotClaude.ResponseDeadlineMs", 10000));
            _groupCooldownMs =
                std::max<int64>(0, sConfigMgr->GetOption<int32>("PlayerbotClaude.GroupCooldownSeconds", 120)) * 1000;
            ConfigureAmbient();

            BridgeConfig config;
            config.port = static_cast<uint16>(port);
            config.token = std::move(*token);
            config.queueCapacity =
                static_cast<uint32>(std::max(1, sConfigMgr->GetOption<int32>("PlayerbotClaude.QueueSize", 16)));
            config.socketTimeoutMs = _responseDeadlineMs;

            std::string const bridgeToken = config.token;

            // Retained because the biography lane validates its own request before enqueueing, and
            // it has no transport object of its own holding the token the way the social lane does.
            _bridgeToken = bridgeToken;

            _bridge = std::make_unique<ClaudeBridge>(std::move(config));
            _bridge->Start();
            if (!PlayerbotCareer::RegisterProvider(this))
            {
                _bridge->Stop();
                _bridge.reset();
                LOG_ERROR("playerbot.claude", "mod-playerbot-claude: career provider registration failed");
                return;
            }
            LOG_INFO("playerbot.claude", "mod-playerbot-claude: bridge worker started on 127.0.0.1:{}", port);

            /*
             * The social provider registers only while the worldserver's social feature is on.
             *
             * Registering regardless would be harmless in itself, since nothing would call it, but it
             * would make "is this module the social provider" a different question from "is social
             * running", and the two answers are read by different people at different times.
             *
             * The gate is read once here rather than per request: SetSocialProvider is the
             * worldserver's own registration seam, and a provider that appeared and vanished as an
             * operator toggled a config would leave outstanding requests pointing at nothing. Turning
             * the feature on or off takes effect at the next startup, which is what the coordinator's
             * own "absence is a supported state" contract already assumes.
             */
            if (PlayerbotSocialConfiguredGate().enabled)
            {
                _socialTransport.emplace(*_bridge, bridgeToken, _responseDeadlineMs);
                sPlayerbotSocialMgr.SetSocialProvider(this);
                LOG_INFO("playerbot.claude", "mod-playerbot-claude: registered as the social provider");
            }
        }

        void Shutdown()
        {
            PlayerbotCareer::UnregisterProvider(this);

            /*
             * Deregistered BEFORE the bridge stops. The coordinator abandons by token rather than
             * expecting a cancellation to be honoured, so the only ordering that matters is that it
             * cannot submit into a bridge that is on its way down.
             */
            if (_socialTransport)
                sPlayerbotSocialMgr.SetSocialProvider(nullptr);

            /*
             * The transport goes before the bridge, not after. It holds a reference to the bridge, so
             * destroying the bridge first would leave that reference dangling even for the moment it
             * takes to drop the exchanges. Every outstanding exchange names a request the coordinator
             * is dropping too, so nothing is lost by forgetting them here.
             */
            _socialTransport.reset();

            if (_bridge)
                _bridge->Stop();
            _bridge.reset();

            _pending.clear();
            _careerPending.clear();
            _careerResponses.clear();
            _ambientCadence.reset();
        }

        /*
         * The social provider seam. Called on the world thread by the coordinator.
         *
         * Everything here that needs the game happens here, and nothing that decides anything does:
         * this resolves two characters into value actors and hands them to the transport, which owns
         * the exchange and every rule about what an answer means. False is ProviderFailed, which the
         * coordinator turns into silence.
         */
        bool Submit(uint64 requestToken, uint64 botGuidCounter, uint64 targetGuidCounter,
                    PlayerbotSocialChannel channel, std::string const& threadPublicId,
                    PlayerbotSocialRequestContext const& context) override
        {
            if (!_socialTransport || !_bridge)
                return false;

            Player* bot = ObjectAccessor::FindPlayer(ObjectGuid::Create<HighGuid::Player>(botGuidCounter));
            if (!bot || !bot->IsInWorld())
                return false;

            SocialRequest request;
            request.socialRequestToken = requestToken;
            request.bot.guidCounter = botGuidCounter;
            request.bot.name = bot->GetName();
            request.bot.human = !GET_PLAYERBOT_AI(bot);
            request.speakOnChannel = static_cast<uint8>(channel);
            request.threadPublicId = threadPublicId;

            /*
             * Everything the coordinator selected for this line: who the bot is, how it feels about
             * the listener, and what it may remember about them. The starter's subject rides in the
             * same value and is empty for a reply, where the thread is the subject.
             *
             * It travels as the assembled context shape rather than as loose text: a context that
             * does not parse is dropped on every channel but a whisper, so anything sent in another
             * shape would reach the prompt nowhere on General, which is the only surface a starter
             * speaks on.
             */
            request.context = ClaudeChat::EncodeSocialContext(context);

            /*
             * The subject is left fully absent when it cannot be resolved, never half described. A
             * guid with no name, or a name for somebody who logged out, would travel as a participant
             * the prompt builder would then describe as present.
             */
            if (targetGuidCounter)
            {
                Player* target = ObjectAccessor::FindPlayer(ObjectGuid::Create<HighGuid::Player>(targetGuidCounter));
                if (target && target->IsInWorld())
                {
                    request.subject.guidCounter = targetGuidCounter;
                    request.subject.name = target->GetName();
                    request.subject.human = !GET_PLAYERBOT_AI(target);
                }
            }

            return _socialTransport->Submit(request);
        }

        /*
         * The biography seam. World thread, same posture as Submit above: resolve, hand over, decide
         * nothing.
         *
         * The identity arrives already resolved by the coordinator rather than being read here. It
         * is still checked against the live character, because the coordinator resolved it on an
         * earlier line of the same tick and this is the last point before it is sent: refusing a
         * request whose character has gone is cheaper than one the far side will answer for nobody.
         */
        bool SubmitBiography(uint64 biographyRequestToken, uint64 botGuidCounter,
                             std::string const& characterName, uint8 raceId, uint8 classId,
                             uint8 genderId) override
        {
            if (!_bridge)
                return false;

            Player* bot = ObjectAccessor::FindPlayer(ObjectGuid::Create<HighGuid::Player>(botGuidCounter));
            if (!bot || !bot->IsInWorld())
                return false;

            ClaudeChat::BiographyRequest request;
            request.biographyRequestToken = biographyRequestToken;
            request.botGuidCounter = botGuidCounter;
            request.characterName = characterName;
            request.raceId = raceId;
            request.classId = classId;
            request.genderId = genderId;

            if (!ClaudeChat::BiographyRequestIsUsable(request, _bridgeToken))
                return false;

            return _bridge->TryEnqueueBiography(std::move(request), SteadyNowMs() + _responseDeadlineMs);
        }

        /*
         * The conversation is already over, so there is no character to check the way a biography
         * checks the bot it is about: the request describes a thread, not a person, and the bot is
         * only who will hold the memory. A bot that logged out between the sweep and here still has
         * durable rows the answer belongs to, so the request is worth sending.
         */
        bool SubmitMemory(uint64 memoryRequestToken, uint64 botGuidCounter,
                          std::string const& threadPublicId, PlayerbotSocialPrivacyScope scope,
                          std::vector<uint64> const& subjectGuidCounters,
                          std::vector<PlayerbotSocialMemoryLine> const& thread) override
        {
            if (!_bridge)
                return false;

            ClaudeChat::MemoryRequest request;
            request.memoryRequestToken = memoryRequestToken;
            request.botGuidCounter = botGuidCounter;
            request.threadPublicId = threadPublicId;
            request.scope = scope;

            Player* bot = ObjectAccessor::FindPlayer(ObjectGuid::Create<HighGuid::Player>(botGuidCounter));
            if (!bot)
                return false;

            // Names are resolved HERE, because turning a guid into one means touching a live
            // character and the coordinator may not. They reach the trusted half of the prompt, so
            // they are read from the character rather than carried through anyone's state.
            request.botName = bot->GetName();

            request.subjects.reserve(subjectGuidCounters.size());
            for (uint64 const subjectGuidCounter : subjectGuidCounters)
            {
                std::string const name = NameOfCharacter(subjectGuidCounter);

                /*
                 * A subject whose name cannot be resolved is dropped rather than sent by guid. A
                 * memory attributed to "character 4712" is one nobody can read, and the name is
                 * going into the trusted instructions. Their lines still travel as context below.
                 */
                if (!name.empty())
                    request.subjects.push_back({subjectGuidCounter, name});
            }

            // Nobody left to be about. Refused here rather than sent for the far side to reject,
            // which is the same fail closed posture every other request shape takes.
            if (request.subjects.empty())
                return false;

            request.thread.reserve(thread.size());
            for (PlayerbotSocialMemoryLine const& line : thread)
            {
                std::string speaker = NameOfCharacter(line.speakerGuidCounter);

                /*
                 * A speaker who has gone is rendered by guid rather than dropped. Removing their
                 * turn would leave the others answering nobody, which changes what the conversation
                 * says; an unresolvable identifier reads as an unknown participant, which is what
                 * they now are.
                 */
                if (speaker.empty())
                    speaker = "character " + std::to_string(line.speakerGuidCounter);

                request.thread.push_back(speaker + ": " + line.text);
            }

            if (!ClaudeChat::MemoryRequestIsUsable(request, _bridgeToken))
                return false;

            return _bridge->TryEnqueueMemory(std::move(request), SteadyNowMs() + _responseDeadlineMs);
        }

        // A character's display name, or empty when it cannot be resolved. Online characters only:
        // an offline lookup is a database round trip, and this runs inside a world tick.
        static std::string NameOfCharacter(uint64 guidCounter)
        {
            Player* character = ObjectAccessor::FindPlayer(ObjectGuid::Create<HighGuid::Player>(guidCounter));
            return character ? character->GetName() : std::string();
        }

        bool TrySubmit(PlayerbotCareerPlanRequest const& careerRequest) override
        {
            if (!_bridge)
                return false;

            Player* bot = ObjectAccessor::FindPlayer(
                ObjectGuid::Create<HighGuid::Player>(careerRequest.botGuid));
            if (!bot || !bot->IsInWorld())
                return false;

            ChatRequest request;
            request.requestId = _nextRequestId++;
            request.channel = ChatChannel::Career;
            request.botGuidCounter = careerRequest.botGuid;
            request.botName = bot->GetName();
            request.profile = careerRequest.profile;
            std::optional<std::string> careerContent = SerializeCareerRequestContent(careerRequest);
            if (!careerContent)
                return false;

            request.message = *std::move(careerContent);
            request.eventKind = CAREER_EVENT_KIND;
            request.expiresAtSteadyMs = SteadyNowMs() + _responseDeadlineMs;

            uint64 const wireRequestId = request.requestId;
            int64 const expiresAtSteadyMs = request.expiresAtSteadyMs;
            if (!_bridge->TryEnqueue(std::move(request)))
                return false;

            _careerPending.emplace(
                wireRequestId,
                PendingCareerDelivery { careerRequest, expiresAtSteadyMs });
            return true;
        }

        std::optional<PlayerbotCareerPlanResponse> Poll(uint64 requestId) override
        {
            auto response = _careerResponses.find(requestId);
            if (response == _careerResponses.end())
                return std::nullopt;

            PlayerbotCareerPlanResponse result = std::move(response->second);
            _careerResponses.erase(response);
            return result;
        }

        uint64 ResponseDeadlineMs() const override
        {
            return static_cast<uint64>(_responseDeadlineMs);
        }

        void CaptureWhisper(Player* speaker, Player* receiver, std::string const& message)
        {
            if (!_bridge)
                return;

            if (!IsRealPlayerActor(speaker) || !IsMachineBot(receiver))
                return;

            // Known playerbot commands keep executing as commands (and cost nothing);
            // only unrecognized whispers become Claude conversation.
            bool isKnownCommand = false;
            if (PlayerbotAI* botAI = GET_PLAYERBOT_AI(receiver))
            {
                ExternalEventHelper helper(botAI->GetAiObjectContext());
                isKnownCommand = helper.IsChatCommand(message);
            }

            std::optional<std::string> text = WhisperClaudeText(message, isKnownCommand);
            if (!text)
                return;

            EnqueueConversation(ChatChannel::Whisper, receiver, speaker, BoundedText(std::move(*text)));
        }

        void CaptureParty(Player* speaker, Group* group, std::string const& message)
        {
            if (!_bridge || !group)
                return;

            std::optional<std::pair<std::string, std::string>> parsed = ParseLlmParty(message);
            if (!parsed)
                return;

            if (!IsRealPlayerActor(speaker))
                return;

            Player* target = nullptr;
            for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
            {
                Player* member = itr->GetSource();
                if (!member || member == speaker || !IsMachineBot(member))
                    continue;

                if (EqualNamesCaseInsensitive(member->GetName(), parsed->first))
                {
                    target = member;
                    break;
                }
            }

            if (!target)
                return;

            EnqueueConversation(ChatChannel::Party, target, speaker, BoundedText(std::move(parsed->second)));
        }

        void CaptureMilestone(Player* actor, uint8 kind, uint64 subjectId, std::string context)
        {
            if (!_bridge)
                return;

            if (!IsRealPlayerActor(actor))
                return;

            Group* group = actor->GetGroup();
            if (!group)
                return;

            std::vector<SpeakerCandidate> candidates;
            std::unordered_map<uint64, Player*> byCounter;
            for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
            {
                Player* member = itr->GetSource();
                if (!member || member == actor || !IsMachineBot(member))
                    continue;

                if (!member->IsInWorld() || !member->IsAlive() || member->IsInCombat())
                    continue;

                uint64 const counter = member->GetGUID().GetCounter();
                PlayerbotPersonalityProfile const profile = PlayerbotPersonality::DeriveProfile(counter);
                candidates.push_back({counter, profile.sociability});
                byCounter[counter] = member;
            }

            if (candidates.empty())
                return;

            uint64 const actorCounter = actor->GetGUID().GetCounter();
            MilestoneEventId eventId;
            eventId.kind = kind;
            eventId.actorGuidCounter = actorCounter;
            eventId.subjectId = subjectId;
            eventId.occurrence = _occurrenceByActor[actorCounter]++;

            if (!_recentEvents.Insert(eventId))
                return;

            if (!_groupCooldowns.TryBegin(group->GetGUID().GetCounter(), SteadyNowMs(), _groupCooldownMs))
                return;

            std::optional<uint64> const selected = SelectMilestoneSpeaker(eventId, candidates);
            if (!selected)
                return;

            Player* bot = byCounter[*selected];
            if (!bot)
                return;

            ChatRequest request = BuildRequestBase(ChatChannel::Party, bot, actor);
            request.message = BoundedText(std::move(context));
            request.eventKind = eventId.kind;
            request.subjectId = eventId.subjectId;
            request.occurrence = eventId.occurrence;
            Enqueue(std::move(request), bot, actor, ChatChannel::Party);
        }

        void Update()
        {
            if (!_bridge)
                return;

            DrainSocial();
            DrainBiographies();
            DrainMemories();
            SweepIdleThreadsForMemories();

            for (ChatResponse const& response : _bridge->DrainResponses())
            {
                auto career = _careerPending.find(response.requestId);
                if (career != _careerPending.end())
                {
                    std::optional<CareerDecision> decision = ParseCareerDecision(response.message);

                    /*
                     * The chosen token has to be one this request actually offered. Parsing only
                     * proves the shape is legal, and accepting an unoffered token would let a
                     * response name a candidate the bot was never given, which is the whole point of
                     * sending an opaque legal set rather than free text.
                     */
                    PlayerbotCareerPlanRequest const& request = career->second.request;
                    bool const offered =
                        decision && std::any_of(request.candidates.begin(), request.candidates.end(),
                                                [&decision](PlayerbotCareerCandidateView const& candidate)
                                                { return candidate.token == decision->candidateToken; });

                    if (decision && !offered)
                        LOG_INFO("playerbot.claude",
                                 "mod-playerbot-claude: career decision for request {} named a candidate "
                                 "that was never offered; discarded",
                                 response.requestId);

                    if (offered)
                    {
                        _careerResponses[request.requestId] = {
                            request.requestId,
                            request.botGuid,
                            request.personalityVersion,
                            request.careerVersion,
                            decision->candidateToken,
                            decision->spendingStyle
                        };
                    }
                    _careerPending.erase(career);
                    continue;
                }

                auto it = _pending.find(response.requestId);
                if (it == _pending.end())
                    continue;

                PendingDelivery const delivery = it->second;
                _pending.erase(it);

                Player* bot = ObjectAccessor::FindPlayer(delivery.botGuid);
                Player* speaker = delivery.channel == ChatChannel::World
                    ? nullptr
                    : ObjectAccessor::FindPlayer(delivery.speakerGuid);

                DeliverySnapshot snapshot;
                snapshot.expired = SteadyNowMs() > delivery.expiresAtSteadyMs;
                snapshot.botOnline = bot && bot->IsInWorld();
                snapshot.speakerOnline = speaker && speaker->IsInWorld();
                snapshot.botIsStillBot = IsMachineBot(bot);
                snapshot.botAlive = bot && bot->IsAlive();
                snapshot.botInCombat = !bot || bot->IsInCombat();
                snapshot.sameGroup =
                    bot && speaker && bot->GetGroup() && bot->GetGroup() == speaker->GetGroup();
                snapshot.humanOnline = delivery.channel == ChatChannel::World && HasOnlineHuman();
                snapshot.worldChannelAvailable = delivery.channel == ChatChannel::World && HasWorldChannel(bot);

                if (!ShouldDeliver(delivery.channel, snapshot))
                {
                    LOG_INFO("playerbot.claude",
                             "mod-playerbot-claude: dropped response for request {} (expired={} botOnline={} "
                             "speakerOnline={} stillBot={} inCombat={} sameGroup={})",
                             response.requestId, snapshot.expired, snapshot.botOnline, snapshot.speakerOnline,
                             snapshot.botIsStillBot, snapshot.botInCombat, snapshot.sameGroup);
                    continue;
                }

                /*
                 * Rechecked here, not only at capture. A request enqueued while the gate was off can
                 * come back after it turned on, and delivering it then would send chat the
                 * coordinator now owns, chosen by the rule that no longer applies. The gate is read
                 * per delivery for the same reason the ambient limiter reads it per tick.
                 */
                if (!LegacyConversationalHookAllowed(PlayerbotSocialConfiguredGate().enabled))
                {
                    LOG_INFO("playerbot.claude",
                             "mod-playerbot-claude: dropped legacy response for request {} "
                             "(AiPlayerbot.SocialChat.Enable took ownership while it was in flight)",
                             response.requestId);
                    continue;
                }

                if (delivery.channel == ChatChannel::World)
                {
                    PlayerbotAI* botAI = GET_PLAYERBOT_AI(bot);
                    if (!botAI || !botAI->SayToWorld(response.message))
                        LOG_INFO("playerbot.claude",
                                 "mod-playerbot-claude: World delivery failed for request {}",
                                 response.requestId);
                }
                else if (delivery.channel == ChatChannel::Whisper)
                    bot->Whisper(response.message, LANG_UNIVERSAL, speaker);
                else
                {
                    WorldPacket data;
                    ChatHandler::BuildChatPacket(data, CHAT_MSG_PARTY, LANG_UNIVERSAL, bot, nullptr,
                                                 response.message.c_str());
                    bot->GetGroup()->BroadcastPacket(&data, false);
                }
            }

            int64 const now = SteadyNowMs();
            std::erase_if(_pending, [now](auto const& entry) { return now > entry.second.expiresAtSteadyMs; });
            std::erase_if(
                _careerPending,
                [now](auto const& entry) { return now > entry.second.expiresAtSteadyMs; });

            // Re-read every tick rather than trusted from startup, so a social gate that becomes
            // live-controllable silences this limiter immediately instead of at the next restart.
            if (LegacyAmbientWorldAllowed(_ambientCadence.has_value(), PlayerbotSocialConfiguredGate().enabled) &&
                _ambientCadence->TryConsumeDueSlot(now))
                TryEnqueueAmbient();
        }

    private:
        void ConfigureAmbient()
        {
            if (!sConfigMgr->GetOption<bool>("PlayerbotClaude.AmbientWorldEnable", false))
                return;

            /*
             * The interactive social feature owns unprompted chatter when it is on, so the hourly
             * limiter is never configured alongside it. Reported rather than silently skipped: this
             * setting being ignored is worth knowing about when reading a running server's log.
             */
            if (!LegacyAmbientWorldAllowed(true, PlayerbotSocialConfiguredGate().enabled))
            {
                LOG_INFO("playerbot.claude",
                         "mod-playerbot-claude: ambient World chat disabled "
                         "(AiPlayerbot.SocialChat.Enable owns unprompted chat while it is on)");
                return;
            }

            int32 const messagesPerHour =
                sConfigMgr->GetOption<int32>("PlayerbotClaude.AmbientMaxMessagesPerHour", 6);
            if (messagesPerHour < 1 ||
                messagesPerHour > static_cast<int32>(MAX_AMBIENT_MESSAGES_PER_HOUR))
            {
                LOG_ERROR("playerbot.claude",
                          "mod-playerbot-claude: ambient World chat disabled "
                          "(PlayerbotClaude.AmbientMaxMessagesPerHour must be from 1 through {})",
                          MAX_AMBIENT_MESSAGES_PER_HOUR);
                return;
            }

            if (sPlayerbotAIConfig.enableBroadcasts)
            {
                LOG_ERROR("playerbot.claude",
                          "mod-playerbot-claude: ambient World chat disabled "
                          "(set AiPlayerbot.EnableBroadcasts = 0)");
                return;
            }

            _ambientCadence.emplace(static_cast<uint32>(messagesPerHour), SteadyNowMs());
            LOG_INFO("playerbot.claude",
                     "mod-playerbot-claude: ambient World chat enabled at up to {} messages per hour",
                     messagesPerHour);
        }

        void TryEnqueueAmbient()
        {
            bool const humanOnline = HasOnlineHuman();
            if (!humanOnline)
                return;

            std::vector<AmbientCandidateSnapshot> snapshots;
            std::vector<SpeakerCandidate> candidates;
            std::unordered_map<uint64, Player*> byCounter;
            for (auto const& entry : sRandomPlayerbotMgr.GetAllBots())
            {
                Player* bot = entry.second;
                AmbientCandidateSnapshot snapshot;
                snapshot.botOnline = bot && bot->IsInWorld();
                snapshot.botAlive = bot && bot->IsAlive();
                snapshot.botIsMachine = IsMachineBot(bot);
                snapshot.botInCombat = !bot || bot->IsInCombat();
                snapshot.worldChannelAvailable = HasWorldChannel(bot);
                snapshots.push_back(snapshot);

                if (!snapshot.botOnline || !snapshot.botAlive || !snapshot.botIsMachine ||
                    snapshot.botInCombat || !snapshot.worldChannelAvailable ||
                    !HasOnlineHumanOnTeam(bot->GetTeamId()))
                    continue;

                uint64 const counter = bot->GetGUID().GetCounter();
                PlayerbotPersonalityProfile const profile = PlayerbotPersonality::DeriveProfile(counter);
                candidates.push_back({counter, profile.sociability});
                byCounter[counter] = bot;
            }

            if (!ShouldEnqueueAmbient(humanOnline, snapshots))
                return;

            uint64 const occurrence = _ambientOccurrence++;
            std::optional<uint64> const selected = SelectAmbientSpeaker(occurrence, candidates);
            if (!selected)
                return;

            auto const selectedIt = byCounter.find(*selected);
            if (selectedIt == byCounter.end())
                return;

            Player* bot = selectedIt->second;
            ChatRequest request;
            request.requestId = _nextRequestId++;
            request.channel = ChatChannel::World;
            request.botGuidCounter = bot->GetGUID().GetCounter();
            request.speakerGuidCounter = 0;
            request.botName = bot->GetName();
            request.speakerName.clear();
            request.profile = PlayerbotPersonality::DeriveProfile(request.botGuidCounter);
            request.message = AMBIENT_EVENT_MARKER;
            request.eventKind = AMBIENT_EVENT_KIND;
            request.occurrence = occurrence;
            request.expiresAtSteadyMs = SteadyNowMs() + _responseDeadlineMs;
            Enqueue(std::move(request), bot, nullptr, ChatChannel::World);
        }

        ChatRequest BuildRequestBase(ChatChannel channel, Player* bot, Player* speaker)
        {
            uint64 const botCounter = bot->GetGUID().GetCounter();

            ChatRequest request;
            request.requestId = _nextRequestId++;
            request.channel = channel;
            request.botGuidCounter = botCounter;
            request.speakerGuidCounter = speaker->GetGUID().GetCounter();
            request.botName = bot->GetName();
            request.speakerName = speaker->GetName();
            request.profile = PlayerbotPersonality::DeriveProfile(botCounter);
            request.expiresAtSteadyMs = SteadyNowMs() + _responseDeadlineMs;
            return request;
        }

        void EnqueueConversation(ChatChannel channel, Player* bot, Player* speaker, std::string text)
        {
            ChatRequest request = BuildRequestBase(channel, bot, speaker);
            request.message = std::move(text);
            request.eventKind = 0;
            Enqueue(std::move(request), bot, speaker, channel);
        }

        /*
         * Hands each generated line to the coordinator, which decides whether it may be spoken.
         *
         * Nothing is delivered here. The coordinator validates the shape, schedules the natural
         * delay, revalidates the world immediately before the send, and can still refuse: this
         * module never speaks, which is Definition of Done 5.
         */
        void DrainSocial()
        {
            if (!_socialTransport)
                return;

            /*
             * Unix milliseconds, which is what the coordinator's delivery clock is denominated in.
             * GetGameTimeMS is milliseconds since server START, and mixing the two would schedule a
             * delivery decades in the past on a freshly restarted realm.
             */
            uint64 const nowMs = PlayerbotSocialUnixMilliseconds(GameTime::GetSystemTime());

            for (ClaudeSocialTransport::Completed const& completed : _socialTransport->Drain())
            {
                if (completed.outcome != SocialExchangeOutcome::Deliver)
                {
                    /*
                     * A regeneration is already back on the wire and an abandonment is final. Neither
                     * is reported to the coordinator, which has no entry point for "this one will not
                     * be answered" and expires the request by its own timeout instead. Task 11 records
                     * the named suppression reason.
                     */
                    continue;
                }

                PlayerbotSocialDeliveryRejection const rejection = sPlayerbotSocialMgr.AcceptSocialResult(
                    completed.result, nowMs, urand(0, 100000));

                if (rejection != PlayerbotSocialDeliveryRejection::None)
                    LOG_DEBUG("playerbot.claude", "mod-playerbot-claude: social result for request {} refused ({})",
                              completed.socialRequestToken, PlayerbotSocialDeliveryRejectionName(rejection));
            }
        }

        /*
         * Hands each generated backstory to the coordinator, which owns the profile and decides
         * whether the answer may be applied.
         *
         * Nothing is stored here. The worker already refused an answer for the wrong request or the
         * wrong bot; what is left is the coordinator's own fence, the field whitelist and the
         * identity check, and all three live on the side that owns the profile.
         */
        /*
         * Asks the coordinator to read whatever conversations have gone quiet, and releases the
         * tokens of any request the bridge never answered.
         *
         * Every decision about what may be read, by whom, and about whom is the coordinator's; this
         * is the tick that makes it happen. The sweep is bounded per call, so a realm where
         * everything falls silent at once produces a few requests per tick rather than hundreds.
         */
        void SweepIdleThreadsForMemories()
        {
            uint64 const nowUnixSeconds = static_cast<uint64>(GameTime::GetGameTime().count());

            sPlayerbotSocialMgr.AbandonStaleMemoryRequests(nowUnixSeconds);
            sPlayerbotSocialMgr.RequestIdleExtractions(nowUnixSeconds);
        }

        /*
         * Hands each extraction answer back to the coordinator.
         *
         * Nothing is stored here and nothing is checked here. The worker already refused an answer
         * for the wrong request or the wrong bot; what remains is the coordinator's own token
         * fence, its subject and scope recheck, and the consent and actor gates inside
         * PersistMemory, all of which live on the side that owns the durable state.
         */
        void DrainMemories()
        {
            if (!_bridge)
                return;

            for (ClaudeChat::MemoryResponse const& response : _bridge->DrainMemoryResponses())
            {
                std::vector<PlayerbotSocialExtractedMemory> extracted;
                extracted.reserve(response.memories.size());

                for (ClaudeChat::MemoryResponseCandidate const& candidate : response.memories)
                    extracted.push_back({candidate.paraphrase, candidate.aboutGuidCounter, candidate.scope});

                sPlayerbotSocialMgr.ApplyExtractedMemories(response.memoryRequestToken,
                                                            response.botGuidCounter, response.threadPublicId,
                                                            extracted);
            }
        }

        void DrainBiographies()
        {
            if (!_bridge)
                return;

            uint64 const nowUnixSeconds = static_cast<uint64>(GameTime::GetGameTime().count());

            for (ClaudeChat::BiographyResponse const& response : _bridge->DrainBiographyResponses())
            {
                /*
                 * Identity is re-resolved from the live character rather than remembered from the
                 * request. The request may have been issued a minute ago, and a biography is
                 * validated against who the character IS, so a stale copy is the wrong thing to
                 * validate against even when it is almost always identical.
                 */
                Player* const bot = ObjectAccessor::FindPlayer(
                    ObjectGuid::Create<HighGuid::Player>(response.botGuidCounter));
                if (!bot || !bot->IsInWorld())
                {
                    // Dropped rather than applied against a remembered identity. The coordinator
                    // times the request out and the bot gets another one later.
                    continue;
                }

                PlayerbotSocialBiographyCandidate authoritative;
                authoritative.botGuidCounter = response.botGuidCounter;
                authoritative.characterName = bot->GetName();
                authoritative.raceId = bot->getRace();
                authoritative.classId = bot->getClass();
                authoritative.genderId = bot->getGender();

                std::vector<PlayerbotBiographyFieldValue> fields;
                fields.reserve(response.fields.size());
                for (ClaudeChat::BiographyResponseField const& field : response.fields)
                    fields.push_back(PlayerbotBiographyFieldValue{field.name, field.value});

                PlayerbotBiographyCompletionRejection const rejection = sPlayerbotSocialMgr.AcceptBiographyResult(
                    response.biographyRequestToken, response.botGuidCounter, fields, authoritative,
                    nowUnixSeconds);

                if (rejection != PlayerbotBiographyCompletionRejection::None)
                    LOG_DEBUG("playerbot.claude",
                              "mod-playerbot-claude: biography for request {} refused ({})",
                              response.biographyRequestToken,
                              PlayerbotBiographyCompletionRejectionName(rejection));
            }
        }

        void Enqueue(ChatRequest request, Player* bot, Player* speaker, ChatChannel channel)
        {
            PendingDelivery delivery;
            delivery.channel = channel;
            delivery.botGuid = bot->GetGUID();
            if (speaker)
                delivery.speakerGuid = speaker->GetGUID();
            delivery.expiresAtSteadyMs = request.expiresAtSteadyMs;

            uint64 const requestId = request.requestId;
            if (_bridge->TryEnqueue(std::move(request)))
                _pending[requestId] = delivery;
        }

        std::unique_ptr<ClaudeBridge> _bridge;

        // Present only while this module is the registered social provider. Holds a reference to the
        // bridge, so it is destroyed before the bridge is and never outlives it.
        std::optional<ClaudeSocialTransport> _socialTransport;

        std::unordered_map<uint64, PendingDelivery> _pending;
        std::unordered_map<uint64, PendingCareerDelivery> _careerPending;
        std::unordered_map<uint64, PlayerbotCareerPlanResponse> _careerResponses;
        std::map<uint64, uint64> _occurrenceByActor;
        RecentEventIdSet _recentEvents{RECENT_EVENT_CAPACITY};
        GroupCooldownTracker _groupCooldowns;
        std::optional<AmbientCadence> _ambientCadence;
        uint64 _nextRequestId = 1;
        std::string _bridgeToken;
        uint64 _ambientOccurrence = 0;
        int64 _responseDeadlineMs = 10000;
        int64 _groupCooldownMs = 120000;
    };

    class ClaudeChatPlayerScript : public PlayerScript
    {
    public:
        ClaudeChatPlayerScript() : PlayerScript("ClaudeChatPlayerScript") { }

        // Observation only: the original message is never modified or blocked, so the
        // playerbot command path sees exactly what it would without this module.
        // LANG_ADDON traffic (DBM sync pings and similar hidden addon whispers) is
        // machine chatter, never conversation.
        bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& message,
                                Player* receiver) override
        {
            /*
             * Yielded to the social coordinator while it is on. Capturing here as well would produce a
             * second, unrelated answer to the same whisper, chosen by a different rule.
             */
            if (type == CHAT_MSG_WHISPER && language != LANG_ADDON &&
                LegacyConversationalHookAllowed(PlayerbotSocialConfiguredGate().enabled))
                ClaudeChatState::Instance().CaptureWhisper(player, receiver, message);

            return true;
        }

        bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& message,
                                Group* group) override
        {
            if (type == CHAT_MSG_PARTY && language != LANG_ADDON &&
                LegacyConversationalHookAllowed(PlayerbotSocialConfiguredGate().enabled))
                ClaudeChatState::Instance().CaptureParty(player, group, message);

            return true;
        }

        void OnPlayerCompleteQuest(Player* player, Quest const* quest) override
        {
            if (!quest || !LegacyConversationalHookAllowed(PlayerbotSocialConfiguredGate().enabled))
                return;

            ClaudeChatState::Instance().CaptureMilestone(player, 1, quest->GetQuestId(),
                                                         "Completed quest: " + quest->GetTitle());
        }

        void OnPlayerLevelChanged(Player* player, uint8 oldLevel) override
        {
            if (!player || player->GetLevel() <= oldLevel ||
                !LegacyConversationalHookAllowed(PlayerbotSocialConfiguredGate().enabled))
                return;

            ClaudeChatState::Instance().CaptureMilestone(player, 2, player->GetLevel(),
                                                         "Reached level " + std::to_string(player->GetLevel()));
        }

        void OnPlayerLootItem(Player* player, Item* item, uint32 /*count*/, ObjectGuid /*lootguid*/) override
        {
            if (!item || !LegacyConversationalHookAllowed(PlayerbotSocialConfiguredGate().enabled))
                return;

            ItemTemplate const* proto = item->GetTemplate();
            if (!proto || proto->Quality < ITEM_QUALITY_RARE || proto->Quality > ITEM_QUALITY_EPIC)
                return;

            ClaudeChatState::Instance().CaptureMilestone(player, 3, proto->ItemId, "Looted: " + proto->Name1);
        }
    };

    class ClaudeChatWorldScript : public WorldScript
    {
    public:
        ClaudeChatWorldScript() : WorldScript("ClaudeChatWorldScript") { }

        void OnStartup() override { ClaudeChatState::Instance().Startup(); }

        void OnShutdown() override { ClaudeChatState::Instance().Shutdown(); }

        void OnUpdate(uint32 /*diff*/) override { ClaudeChatState::Instance().Update(); }
    };
}

void AddClaudeChatScripts()
{
    new ClaudeChatPlayerScript();
    new ClaudeChatWorldScript();
}
