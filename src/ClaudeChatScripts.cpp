/*
 * This file is part of the mod-playerbot-claude module.
 */

#include "ClaudeChat.h"

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

    bool HasWorldChannel(Player* player)
    {
        if (!player)
            return false;

        ChannelMgr* channelManager = ChannelMgr::forTeam(player->GetTeamId());
        return channelManager && channelManager->GetChannel("World", player);
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
    class ClaudeChatState : public PlayerbotCareerPlanProvider
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
        }

        void Shutdown()
        {
            PlayerbotCareer::UnregisterProvider(this);
            if (_bridge)
                _bridge->Stop();
            _bridge.reset();
            _pending.clear();
            _careerPending.clear();
            _careerResponses.clear();
            _ambientCadence.reset();
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
            request.message = SerializeCareerRequestContent(careerRequest);
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

            for (ChatResponse const& response : _bridge->DrainResponses())
            {
                auto career = _careerPending.find(response.requestId);
                if (career != _careerPending.end())
                {
                    std::optional<CareerDecision> decision = ParseCareerDecision(response.message);
                    if (decision)
                    {
                        PlayerbotCareerPlanRequest const& request = career->second.request;
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

            if (_ambientCadence && _ambientCadence->TryConsumeDueSlot(now))
                TryEnqueueAmbient();
        }

    private:
        void ConfigureAmbient()
        {
            if (!sConfigMgr->GetOption<bool>("PlayerbotClaude.AmbientWorldEnable", false))
                return;

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
            for (Player* bot : sRandomPlayerbotMgr.GetPlayers())
            {
                AmbientCandidateSnapshot snapshot;
                snapshot.botOnline = bot && bot->IsInWorld();
                snapshot.botAlive = bot && bot->IsAlive();
                snapshot.botIsMachine = IsMachineBot(bot);
                snapshot.botInCombat = !bot || bot->IsInCombat();
                snapshot.worldChannelAvailable = HasWorldChannel(bot);
                snapshots.push_back(snapshot);

                if (!snapshot.botOnline || !snapshot.botAlive || !snapshot.botIsMachine ||
                    snapshot.botInCombat || !snapshot.worldChannelAvailable)
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
        std::unordered_map<uint64, PendingDelivery> _pending;
        std::unordered_map<uint64, PendingCareerDelivery> _careerPending;
        std::unordered_map<uint64, PlayerbotCareerPlanResponse> _careerResponses;
        std::map<uint64, uint64> _occurrenceByActor;
        RecentEventIdSet _recentEvents{RECENT_EVENT_CAPACITY};
        GroupCooldownTracker _groupCooldowns;
        std::optional<AmbientCadence> _ambientCadence;
        uint64 _nextRequestId = 1;
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
            if (type == CHAT_MSG_WHISPER && language != LANG_ADDON)
                ClaudeChatState::Instance().CaptureWhisper(player, receiver, message);

            return true;
        }

        bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& message,
                                Group* group) override
        {
            if (type == CHAT_MSG_PARTY && language != LANG_ADDON)
                ClaudeChatState::Instance().CaptureParty(player, group, message);

            return true;
        }

        void OnPlayerCompleteQuest(Player* player, Quest const* quest) override
        {
            if (!quest)
                return;

            ClaudeChatState::Instance().CaptureMilestone(player, 1, quest->GetQuestId(),
                                                         "Completed quest: " + quest->GetTitle());
        }

        void OnPlayerLevelChanged(Player* player, uint8 oldLevel) override
        {
            if (!player || player->GetLevel() <= oldLevel)
                return;

            ClaudeChatState::Instance().CaptureMilestone(player, 2, player->GetLevel(),
                                                         "Reached level " + std::to_string(player->GetLevel()));
        }

        void OnPlayerLootItem(Player* player, Item* item, uint32 /*count*/, ObjectGuid /*lootguid*/) override
        {
            if (!item)
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
