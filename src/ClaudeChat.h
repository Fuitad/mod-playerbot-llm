/*
 * This file is part of the mod-playerbot-claude module.
 */

#ifndef MOD_PLAYERBOT_CLAUDE_CLAUDECHAT_H
#define MOD_PLAYERBOT_CLAUDE_CLAUDECHAT_H

#include "PlayerbotPersonality.h"
#include "PlayerbotCareerPlan.h"

#include "Bot/Social/PlayerbotSocialProvider.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

// Bounded bridge between world-thread game hooks and the loopback Claude sidecar.
//
// Trust boundaries: requests carry only immutable value snapshots (GUID counters, names,
// profiles, strings, timestamps). No live game pointer or game API ever crosses into the
// bridge worker. Responses carry text only; nothing in a response can invoke gameplay.
namespace ClaudeChat
{
    /*
     * Bumped to 3 for the social protocol: a typed actor, a response kind, and a social request
     * variant. A sidecar speaking 2 is rejected outright rather than partially understood, which is
     * what "fail closed on a mismatched protocol" means here.
     */
    inline constexpr uint32 SCHEMA_VERSION = 4;
    inline constexpr size_t FRAME_HEADER_BYTES = 4;
    inline constexpr size_t MAX_FRAME_PAYLOAD_BYTES = 64 * 1024;
    inline constexpr size_t MAX_RESPONSE_MESSAGE_BYTES = 240;
    inline constexpr size_t MIN_BRIDGE_TOKEN_BYTES = 32;
    /*
     * The token has a floor for entropy and now a ceiling for the same reason every other string
     * here does. Equality checks and the frame ceiling bounded it only incidentally: a very long
     * token would still be copied, compared, and serialized into every request before anything
     * refused it, and the rule this protocol claims is that no string is bounded incidentally.
     */
    inline constexpr size_t MAX_BRIDGE_TOKEN_BYTES = 256;
    inline constexpr size_t MAX_REQUEST_MESSAGE_BYTES = 512;
    inline constexpr size_t MAX_CAREER_MESSAGE_BYTES = 60 * 1024;
    inline constexpr size_t MAX_CAREER_TOKEN_BYTES = 64;
    inline constexpr size_t MAX_CAREER_SUMMARY_BYTES = 160;

    // Whether a token is usable at all. Both bounds, checked before it is carried anywhere.
    [[nodiscard]] bool BridgeTokenIsUsable(std::string const& token);
    inline constexpr uint32 MAX_AMBIENT_MESSAGES_PER_HOUR = 60;
    inline constexpr uint8 AMBIENT_EVENT_KIND = 4;
    inline constexpr uint8 CAREER_EVENT_KIND = 5;
    inline constexpr char AMBIENT_EVENT_MARKER[] = "ambient_world";

    enum class ChatChannel : uint8
    {
        Whisper = 0,
        Party = 1,
        World = 2,
        Career = 3,
        // Everything the social coordinator asks for. The channel it should be SPOKEN on travels in
        // the social request itself, because that is the worldserver's decision and not this
        // bridge's.
        Social = 4
    };

    /*
     * What a response claims to be.
     *
     * Career and social answers travel the same socket and are told apart by this rather than by
     * shape, so a career decision can never be handed to the social coordinator as a line to speak,
     * and a social line can never be read as a crafting choice. Definition of Done 3 and 4.
     */
    enum class ResponseKind : uint8
    {
        Chat = 0,
        Career,
        Social,
        Biography,
        Memory
    };

    [[nodiscard]] bool ResponseKindIsValid(ResponseKind kind);
    [[nodiscard]] char const* ResponseKindName(ResponseKind kind);
    [[nodiscard]] std::optional<ResponseKind> ResponseKindFromName(std::string const& name);

    /*
     * One participant, however they are driven.
     *
     * Definition of Done 2: a bot speaker and a human speaker serialize through this same structure.
     * Two shapes would let a prompt builder treat the two differently by accident, and the contract
     * is explicit that a human's priority comes from being actively engaged rather than from being
     * human, which only holds if both are described identically.
     */
    struct Actor
    {
        uint64 guidCounter = 0;
        std::string name;
        bool human = false;
    };

    /*
     * An actor is either fully present or fully absent, never half described.
     *
     * A zero guid with a name still attached is an orphaned combination: nothing can resolve it, but
     * it still travels, and a prompt builder reading the name would describe a participant that is
     * not there. Absence has to mean absence.
     */
    [[nodiscard]] bool ActorIsAbsent(Actor const& actor);

    inline constexpr size_t MAX_ACTOR_NAME_BYTES = 48;

    /*
     * `MAX_PLAYER_NAME` from ObjectMgr.h, the client's own limit on a character name.
     *
     * A separate bound from the byte budget above, and neither replaces the other. 48 bytes is
     * what the frame can carry, and it equals twelve characters only in the worst case of a four
     * byte script, so on its own it admits 48 Latin letters. That is enough to spell an
     * instruction with no spaces in it, and the name is interpolated into the sidecar's trusted
     * prompt, so the character count is a security bound rather than a formatting one.
     */
    inline constexpr size_t MAX_ACTOR_NAME_CHARACTERS = 12;

    // Refuses an actor that could not name a real character, so nothing unbounded or anonymous
    // reaches the sidecar.
    [[nodiscard]] bool ActorIsUsable(Actor const& actor);

    /*
     * A social generation, requested by the worldserver's coordinator.
     *
     * `socialRequestToken` is the coordinator's token, echoed back so a stale answer can be refused
     * by identity rather than by guesswork. `speakOnChannel` is the coordinator's decision about
     * where the line belongs and is carried so the response can be checked against it.
     */
    struct SocialRequest
    {
        uint64 socialRequestToken = 0;
        Actor bot;
        Actor subject;
        uint8 speakOnChannel = 0;
        std::string threadPublicId;
        std::string context;
    };

    inline constexpr size_t MAX_SOCIAL_CONTEXT_BYTES = 4 * 1024;
    inline constexpr size_t MAX_THREAD_ID_BYTES = 64;

    /*
     * One entry inside the assembled context, mirroring the sidecar's per field bound rather than
     * the whole context's. A field longer than this is refused there, so bounding it here is what
     * keeps an oversize entry from costing a round trip to find that out.
     */
    inline constexpr size_t MAX_SOCIAL_CONTEXT_ENTRY_BYTES = 512;

    /*
     * At most one regeneration for a fresh invalid response.
     *
     * A sidecar that keeps returning malformed output would otherwise be retried indefinitely on
     * one request. One retry covers a transient glitch; the coordinator decides whether a second
     * request is worth making at all.
     */
    inline constexpr uint32 MAX_REGENERATIONS_PER_REQUEST = 1;

    // Immutable snapshot captured on the world thread. expiresAtSteadyMs is a steady-clock
    // deadline used for queue expiry and is never serialized.
    struct ChatRequest
    {
        uint64 requestId = 0;
        ChatChannel channel = ChatChannel::Whisper;
        uint64 botGuidCounter = 0;
        uint64 speakerGuidCounter = 0;
        std::string botName;
        std::string speakerName;
        PlayerbotPersonalityProfile profile{};
        std::string message;
        uint8 eventKind = 0;   // 0 conversation, 1 quest completion, 2 level gain, 3 rare loot
        uint64 subjectId = 0;
        uint64 occurrence = 0;
        int64 expiresAtSteadyMs = 0;
    };

    struct ChatResponse
    {
        uint64 requestId = 0;
        std::string message;
        ResponseKind kind = ResponseKind::Chat;
    };

    /*
     * A social answer, after parsing but before the coordinator sees it.
     *
     * Carries the coordinator's own token and the channel the sidecar believes it is answering on,
     * so both can be checked against the request rather than trusted. `regenerate` is the sidecar
     * indicating its own output was unusable; it is honoured at most MAX_REGENERATIONS_PER_REQUEST
     * times.
     */
    /*
     * The gestures a social answer may carry, as WoW `TextEmotes` values.
     *
     * The sidecar restricts the model to these by name, but that is a rule on the side that can be
     * forged, replaced, or simply wrong, and the coordinator only refuses zero. Enforced here as
     * well, where the value is READ, so an arbitrary emote cannot be delivered by a response that
     * merely claims one. Must stay in step with SOCIAL_EMOTES in the sidecar's protocol.py; a test
     * asserts the two sets match.
     */
    inline constexpr std::array<uint32, 15> SOCIAL_EMOTE_IDS = {5,  17, 21, 23, 48,  49,  60, 67,
                                                                78, 83, 85, 97, 101, 120, 163};

    [[nodiscard]] bool SocialEmoteIsSupported(uint32 emoteId);

    struct SocialResponse
    {
        uint64 socialRequestToken = 0;
        uint64 botGuidCounter = 0;
        uint8 speakOnChannel = 0;
        std::string message;

        // A WoW text emote ID, or zero for a line. Exactly one of this and `message` is set on a
        // deliverable answer: a gesture and a line are two answers to one question, and the
        // coordinator would drop the text anyway.
        uint32 emoteId = 0;
        bool regenerate = false;
    };

    /*
     * A social answer as it came off the wire, still unparsed, tagged with the request it claims to
     * answer.
     *
     * The bridge worker deliberately does NOT parse it. Classifying a social payload spends the
     * request's regeneration budget, and that budget is per request state owned by the transport on
     * the world thread. A worker that parsed would either have to carry the budget across a thread
     * boundary or answer without it, and the second is how one malformed sidecar retries forever.
     */
    struct SocialRawResponse
    {
        uint64 socialRequestToken = 0;
        std::string payload;

        /*
         * Stamped by the worker when the answer is queued, not read when the world thread gets
         * around to it. Whether a response beat its deadline is a fact about when it ARRIVED, and
         * the two clocks can be a whole tick apart. Judging by drain time would discard an answer
         * that was in time and accept one that was not, depending only on tick alignment.
         */
        int64 receivedAtSteadyMs = 0;
    };

/*
     * Tracks one social request across the bridge, and decides what to do with what comes back.
     *
     * Pure state: it holds the identity of the outstanding request and the regeneration count, and
     * answers "may this response be delivered, should I ask again, or is this over". The socket, the
     * queue, and the coordinator all live outside it, which is what makes every rule here testable
     * without either of them.
     */
    enum class SocialExchangeOutcome : uint8
    {
        Deliver = 0,     // A usable line for the coordinator.
        Regenerate,      // The sidecar asked to try again, and it is allowed to.
        Abandon          // Refused, exhausted, or not ours. Nothing is delivered.
    };

    [[nodiscard]] bool SocialExchangeOutcomeIsValid(SocialExchangeOutcome outcome);

    class SocialExchange
    {
    public:
        SocialExchange(uint64 socialRequestToken, uint64 botGuidCounter)
            : _socialRequestToken(socialRequestToken), _botGuidCounter(botGuidCounter)
        {
        }

        [[nodiscard]] uint64 SocialRequestToken() const { return _socialRequestToken; }
        [[nodiscard]] uint32 Regenerations() const { return _regenerations; }

        /*
         * Classifies one raw payload against this exchange.
         *
         * Fails closed on every path: an unparseable payload, an answer for a different request or
         * bot, and an exhausted regeneration budget all abandon rather than deliver. `out` is only
         * written when the outcome is Deliver.
         */
        SocialExchangeOutcome Classify(std::string const& payload, std::string const& expectedToken,
                                       SocialResponse& out);

    private:
        uint64 _socialRequestToken = 0;
        uint64 _botGuidCounter = 0;
        uint32 _regenerations = 0;
    };

    /*
     * Serializes a social request, or refuses it.
     *
     * Validates before it writes: both actors usable, and the thread identity and context inside
     * their byte budgets. std::string::size() is already a byte count in C++, so these are the same
     * bounds the sidecar enforces rather than a looser character version of them. A refusal returns
     * nullopt rather than an oversize frame the far side would reject anyway, so the failure happens
     * here where the caller can see which request caused it.
     */
    /*
     * Whether a social request could be serialized at all: both actors, the thread identity, the
     * context, and the bridge token, each inside its byte budget.
     *
     * Named separately so the transport can refuse a request without building the frame twice, and
     * so there is exactly ONE definition of a usable request. Two would drift, and the one that
     * drifted would be the one that only the far side enforced.
     */
    [[nodiscard]] bool SocialRequestIsUsable(SocialRequest const& request, std::string const& token);

    /*
     * How long a social exchange may stay outstanding, given what the operator configured.
     *
     * `PlayerbotClaude.ResponseDeadlineMs` has only a floor, so nothing stops it being set above the
     * coordinator's own provider timeout. Past that point the coordinator has already abandoned the
     * request as timed out while this side still holds the slot, so the transport would sit at its
     * bound refusing new work on behalf of requests nobody is waiting for. The coordinator's timeout
     * is therefore the ceiling, and it is named here rather than inlined so it is testable without
     * waiting out a real one.
     */
    [[nodiscard]] int64 SocialRequestDeadlineMs(int64 configuredDeadlineMs);

    std::optional<std::string> SerializeSocialRequest(SocialRequest const& request, std::string const& token);

    /*
     * A starter's subject as the context shape the sidecar parses, or empty for a reply.
     *
     * Not the raw subject. An unparseable context is dropped on every channel but a whisper, which
     * is a deliberate privacy rule and not something to work around, so loose text would arrive
     * nowhere on General: exactly the surface starters use.
     *
     * The subject is truncated rather than refused, and truncated on a character boundary. Losing
     * the tail of what a bot meant to bring up is a smaller failure than the bot saying nothing;
     * splitting a character is not, because the sidecar decodes the whole frame as UTF-8 before it
     * reads any field and would refuse the request outright.
     */
    [[nodiscard]] std::string EncodeStarterContext(std::string const& subject);

    /*
     * A request for one bot's backstory.
     *
     * Carries the identity rather than asking for it. The generated reply has nowhere to put a
     * name, race, class or gender, so those travel out here and are stamped back on afterwards:
     * a generated value can then never become an identity, because the model is never asked.
     *
     * `biographyRequestToken` is the half of the staleness rule the profile's own state cannot
     * supply. Requiring the profile to still be Pending stops a completion replacing a biography
     * that is already Ready, but it cannot say WHICH request a completion answers, so after the
     * pending timeout and a fresh request a very late reply to the superseded call still finds
     * the profile Pending. Only a token minted per request and echoed back closes that.
     */
    struct BiographyRequest
    {
        uint64 biographyRequestToken = 0;
        uint64 botGuidCounter = 0;
        std::string characterName;
        uint8 raceId = 0;
        uint8 classId = 0;
        uint8 genderId = 0;
    };

    [[nodiscard]] bool BiographyRequestIsUsable(BiographyRequest const& request, std::string const& token);

    std::optional<std::string> SerializeBiographyRequest(BiographyRequest const& request,
                                                         std::string const& token);

    /*
     * The generated field names a biography response may carry, and the only ones.
     *
     * Must stay in step with BIOGRAPHY_FIELD_NAMES in the sidecar's protocol.py, which asserts its
     * own reply model against that tuple at import. Identity names are deliberately absent: the
     * worldserver stamps name, race, class and gender from its own character tables, so a payload
     * offering one is refused here rather than trusted.
     */
    inline constexpr std::array<char const*, 8> BIOGRAPHY_FIELD_NAMES = {
        "origin",   "motivation",       "formative_experience", "interests",
        "aversions", "preferred_topics", "mannerisms",           "values"};

    // Matches PLAYERBOT_SOCIAL_BIOGRAPHY_MAX_FIELD_LENGTH in mod-playerbots and
    // MAX_BIOGRAPHY_FIELD_BYTES in the sidecar. Anything longer is prose where a field was asked
    // for, and is refused at the boundary rather than truncated into the profile.
    inline constexpr std::size_t MAX_BIOGRAPHY_FIELD_BYTES = 240;

    // One generated field, as it arrived. Deliberately name and value rather than a typed
    // biography: this module transports, and the assembler in mod-playerbots is what decides
    // which names are legal and fills the identity. Duplicating that decision here would be a
    // second whitelist to keep in step.
    struct BiographyResponseField
    {
        std::string name;
        std::string value;
    };

    struct BiographyResponse
    {
        uint64 biographyRequestToken = 0;
        uint64 botGuidCounter = 0;

        // In the contract's own order, not the parser's map order, so a consumer that cares can
        // rely on it and a test can pin position as well as content.
        std::vector<BiographyResponseField> fields;
    };

    /*
     * Reads one biography answer off the wire.
     *
     * Identity before content, as the social parser does: a well formed answer to a DIFFERENT
     * request, or for a different bot, is refused rather than handed to whoever is waiting. The
     * declared kind is checked before anything is read out, because a biography and a social line
     * share a token and a bot guid, and telling them apart by shape is how a backstory gets spoken.
     */
    std::optional<BiographyResponse> ParseBiographyResponsePayload(std::string const& payload,
                                                                   std::string const& expectedToken,
                                                                   uint64 expectedRequestToken,
                                                                   uint64 expectedBotGuidCounter);

    /*
     * Strict parser for a social answer.
     *
     * Checks the schema version, the token, the response kind, the coordinator's request token, and
     * the bot identity before returning anything, so a well formed answer to a DIFFERENT request, or
     * a career decision wearing a social shape, is refused rather than delivered. Everything else is
     * rejected: unknown fields, duplicate keys, a mismatched kind, an oversize message.
     */
    std::optional<SocialResponse> ParseSocialResponsePayload(std::string const& payload,
                                                             std::string const& expectedToken,
                                                             uint64 expectedRequestToken,
                                                             uint64 expectedBotGuidCounter);

    // Milliseconds on the steady clock; the only time source used for expiry.
    int64 SteadyNowMs();

    // Truncates to at most maxBytes without splitting a UTF-8 sequence. Input that is
    // already invalid UTF-8 is cut at its first invalid byte.
    std::string TruncateUtf8Bytes(std::string text, size_t maxBytes);

    // Reads PLAYERBOT_CLAUDE_BRIDGE_TOKEN. Fails closed (nullopt) when the variable is
    // missing or shorter than MIN_BRIDGE_TOKEN_BYTES.
    std::optional<std::string> BridgeTokenFromEnvironment();

    // 4-byte network-order length prefix plus UTF-8 JSON payload. Rejects payloads over
    // MAX_FRAME_PAYLOAD_BYTES.
    std::optional<std::vector<uint8>> EncodeFrame(std::string const& payload);
    std::optional<uint32> DecodeFrameLength(std::array<uint8, FRAME_HEADER_BYTES> const& header);

    // Serializes a request with a fixed field order. The token authenticates the frame to
    // the sidecar and never appears in logs.
    // Refuses rather than building a frame the far side will reject: the token, both names, and the
    // message are each held to their byte budget here, matching what the sidecar enforces.
    std::optional<std::string> SerializeRequest(ChatRequest const& request, std::string const& token);

    // Strict parser for sidecar responses. Accepts exactly the contract fields with a
    // matching schema version and token, one line of at most MAX_RESPONSE_MESSAGE_BYTES of
    // valid UTF-8, and rejects everything else.
    std::optional<ChatResponse> ParseResponsePayload(std::string const& payload, std::string const& expectedToken);

    struct CareerDecision
    {
        std::string candidateToken;
        PlayerbotRecipeSpendingStyle spendingStyle = PlayerbotRecipeSpendingStyle::None;
    };

    // Same rule for the career payload: every candidate token and summary is bounded here as well as
    // in the sidecar, so neither side is the only thing standing between a model and an oversize frame.
    std::optional<std::string> SerializeCareerRequestContent(PlayerbotCareerPlanRequest const& request);
    std::optional<CareerDecision> ParseCareerDecision(std::string const& content);

    // Bounded FIFO shared between the world thread and the bridge worker. A full or
    // stopped queue rejects immediately; nothing ever blocks the pushing side.
    template <typename T>
    class BoundedQueue
    {
    public:
        explicit BoundedQueue(uint32 capacity) : _capacity(capacity) { }

        bool TryPush(T value)
        {
            {
                std::lock_guard<std::mutex> lock(_mutex);
                if (_stopped || _items.size() >= _capacity)
                    return false;

                _items.push_back(std::move(value));
            }
            _cv.notify_one();
            return true;
        }

        bool TryPop(T& out)
        {
            std::lock_guard<std::mutex> lock(_mutex);
            if (_stopped || _items.empty())
                return false;

            out = std::move(_items.front());
            _items.pop_front();
            return true;
        }

        // Blocks up to timeout for an item; false on timeout or stop.
        bool WaitPop(T& out, std::chrono::milliseconds timeout)
        {
            std::unique_lock<std::mutex> lock(_mutex);
            _cv.wait_for(lock, timeout, [this]() { return _stopped || !_items.empty(); });
            if (_stopped || _items.empty())
                return false;

            out = std::move(_items.front());
            _items.pop_front();
            return true;
        }

        void Stop()
        {
            {
                std::lock_guard<std::mutex> lock(_mutex);
                _stopped = true;
            }
            _cv.notify_all();
        }

    private:
        mutable std::mutex _mutex;
        std::condition_variable _cv;
        std::deque<T> _items;
        uint32 _capacity;
        bool _stopped = false;
    };

    // --- Milestone events and speaker selection ---

    inline constexpr uint32 MILESTONE_SELECTION_VERSION = 1;

    // Identifies one eligible milestone occurrence. Kind is 1 for quest completion, 2 for
    // level gain, and 3 for rare or epic loot; subjectId is the quest ID, new level, or
    // item entry respectively. Occurrence is a world-thread, per-actor monotonic counter
    // (reset on restart).
    struct MilestoneEventId
    {
        uint8 kind = 0;
        uint64 actorGuidCounter = 0;
        uint64 subjectId = 0;
        uint64 occurrence = 0;

        bool operator==(MilestoneEventId const& other) const = default;
    };

    struct SpeakerCandidate
    {
        uint64 guidCounter = 0;
        uint8 sociability = 0;
    };

    // Deterministic weighted choice: candidates are sorted by GUID counter, each weighted
    // 1 + sociability, and the roll is derived from the event identifier with the shared
    // SplitMix64 chain. The same event and candidate set always select the same bot.
    std::optional<uint64> SelectMilestoneSpeaker(MilestoneEventId const& eventId,
                                                 std::vector<SpeakerCandidate> candidates);

    // Deterministic personality-weighted choice for one ambient occurrence. The
    // selected bot is stable for the same occurrence and eligible candidate set.
    std::optional<uint64> SelectAmbientSpeaker(uint64 occurrence, std::vector<SpeakerCandidate> candidates);

    // Local steady-clock scheduler. A newly configured cadence waits one full
    // interval, and a late evaluation advances from now so no catch-up burst occurs.
    class AmbientCadence
    {
    public:
        AmbientCadence(uint32 messagesPerHour, int64 startMs);

        bool IsValid() const;
        bool TryConsumeDueSlot(int64 nowMs);

    private:
        int64 _intervalMs = 0;
        int64 _nextDueMs = 0;
    };

    struct AmbientCandidateSnapshot
    {
        bool botOnline = false;
        bool botAlive = false;
        bool botIsMachine = false;
        bool botInCombat = true;
        bool worldChannelAvailable = false;
    };

    // True only when a human is online and at least one candidate passes every
    // preflight gate required for a World request.
    bool ShouldEnqueueAmbient(bool humanOnline, std::vector<AmbientCandidateSnapshot> const& candidates);

    // True only when the legacy hourly ambient World limiter may still run: it is configured on AND
    // the interactive social feature is off. The two are competing answers to when a bot speaks
    // unprompted, so the older one yields while the newer one owns the decision.
    bool LegacyAmbientWorldAllowed(bool ambientConfigured, bool socialGateEnabled);

    /*
     * True only when a legacy conversational hook may still act.
     *
     * The direct whisper, explicit party, and milestone captures each select a responder and send
     * chat on their own. While the social feature is on, the worldserver's coordinator owns both of
     * those decisions, so every one of these yields to it rather than producing a second, unrelated
     * answer to the same message. Definition of Done 1.
     *
     * Kept rather than deleted, because gate off compatibility is a requirement: with the social
     * feature disabled these are still the only thing that answers a whisper.
     */
    bool LegacyConversationalHookAllowed(bool socialGateEnabled);

    // Bounded set of recently enqueued event identifiers with FIFO eviction. Insert
    // returns false for an exact duplicate still tracked.
    class RecentEventIdSet
    {
    public:
        explicit RecentEventIdSet(size_t capacity) : _capacity(capacity) { }

        bool Insert(MilestoneEventId const& eventId);

    private:
        size_t _capacity;
        std::deque<MilestoneEventId> _order;
    };

    // --- Explicit chat capture ---

    // Whisper routing: explicit "llm <text>" always goes to Claude (even command-shaped
    // text); a malformed explicit attempt ("llm", "llm ") stays silent. Unprefixed text
    // goes to Claude only when the playerbot command system does not recognize it, so
    // commands keep costing nothing.
    std::optional<std::string> WhisperClaudeText(std::string const& message, bool isKnownPlayerbotCommand);

    // "llm <message>" in a direct whisper: returns the message. Anything else, including
    // different case or leading whitespace, is not a capture.
    std::optional<std::string> ParseLlmWhisper(std::string const& message);

    // "llm <bot-name> <message>" in party chat: returns the named bot and the message.
    std::optional<std::pair<std::string, std::string>> ParseLlmParty(std::string const& message);

    // --- Delivery policy ---

    // Snapshot of revalidated world state taken on the world thread immediately before
    // delivery. All fields default to the failing side so a forgotten field cannot leak
    // a delivery.
    struct DeliverySnapshot
    {
        bool botOnline = false;
        bool speakerOnline = false;
        bool botIsStillBot = false;
        bool botAlive = false;
        bool botInCombat = true;
        bool sameGroup = false;
        bool humanOnline = false;
        bool worldChannelAvailable = false;
        bool expired = true;
    };

    // True only when every policy gate passes for the channel. Party delivery
    // additionally requires speaker and bot to still share a group.
    bool ShouldDeliver(ChatChannel channel, DeliverySnapshot const& snapshot);

    // One milestone reaction per group per cooldown window.
    class GroupCooldownTracker
    {
    public:
        // Returns true and records nowMs when the group is off cooldown.
        bool TryBegin(uint64 groupId, int64 nowMs, int64 cooldownMs);

    private:
        std::map<uint64, int64> _lastBeginMs;
    };

    struct BridgeConfig
    {
        std::string host = "127.0.0.1";
        uint16 port = 0;
        std::string token;
        uint32 queueCapacity = 16;
        int64 socketTimeoutMs = 1000;
    };

    // One reconnecting worker thread draining the request queue. Game hooks only ever
    // TryEnqueue and DrainResponses; they never wait for network or disk I/O. Stop closes
    // the socket so shutdown never waits out a read deadline. Failed or expired requests
    // are dropped without a fabricated response.
    class ClaudeBridge
    {
    public:
        explicit ClaudeBridge(BridgeConfig config);
        ~ClaudeBridge();

        ClaudeBridge(ClaudeBridge const&) = delete;
        ClaudeBridge& operator=(ClaudeBridge const&) = delete;

        void Start();
        void Stop();

        // World thread: false when the bridge is stopped, the queue is full, or the
        // request is already expired.
        bool TryEnqueue(ChatRequest request);

        /*
         * World thread: the social lane, sharing this one socket and worker.
         *
         * A second bridge would mean a second connection, a second reconnect loop, and two
         * independent queue bounds for one sidecar. The two shapes share everything except how they
         * are serialized and what comes back, so they share the queue and differ there.
         */
        bool TryEnqueueSocial(SocialRequest request, int64 expiresAtSteadyMs);

        // World thread: returns every completed response since the previous drain.
        std::vector<ChatResponse> DrainResponses();
        std::vector<SocialRawResponse> DrainSocialResponses();

    private:
        struct Impl;
        std::unique_ptr<Impl> _impl;
    };

    /*
     * The social transport: everything the provider does that does not need a game object.
     *
     * Split from the provider itself for the reason every other layer here is split: the worldserver
     * coordinator cannot be linked into this module's unit tests, so a rule that lives inside the
     * provider is a rule nothing executes. This half owns the outstanding exchanges and decides what
     * each answer is; the provider half resolves characters into actors and talks to the coordinator.
     *
     * Nothing here holds a game pointer, and nothing here delivers. It produces a value the
     * worldserver then judges, which is Definition of Done 5.
     */
    inline constexpr std::size_t MAX_OUTSTANDING_SOCIAL_REQUESTS =
        PLAYERBOT_SOCIAL_MAX_PENDING_BOTS * PLAYERBOT_SOCIAL_MAX_PENDING_PER_BOT;

    class ClaudeSocialTransport
    {
    public:
        // Capped here rather than at the call site, so the invariant stays with the class that
        // depends on it and a later caller cannot reintroduce the mismatch.
        ClaudeSocialTransport(ClaudeBridge& bridge, std::string bridgeToken, int64 requestDeadlineMs)
            : _bridge(bridge), _bridgeToken(std::move(bridgeToken)),
              _requestDeadlineMs(SocialRequestDeadlineMs(requestDeadlineMs))
        {
        }

        ClaudeSocialTransport(ClaudeSocialTransport const&) = delete;
        ClaudeSocialTransport& operator=(ClaudeSocialTransport const&) = delete;

        /*
         * Opens an exchange and hands the request to the bridge.
         *
         * False on every refusal, which the coordinator reads as ProviderFailed and turns into
         * silence: an unusable actor, a duplicate token, a full transport, an unusable bridge token,
         * and a full or stopped queue are all "this will not be answered", and saying so immediately
         * is better than holding a slot until the coordinator's own timeout expires it.
         */
        bool Submit(SocialRequest const& request);

        /*
         * `Submit` with the instant handed in, the same split as `Drain` and `Resolve`. The
         * exchange's deadline is then exactly `nowMs + deadline` rather than that plus however long
         * the caller took to reach this line, which is what lets a test state a margin instead of
         * assuming one.
         */
        bool SubmitAt(SocialRequest const& request, int64 nowMs);

        /*
         * One drained answer, already classified.
         *
         * `result` is only meaningful for Deliver. Regenerate means the transport has already asked
         * again and there is nothing yet to do; Abandon means this request will never produce a line,
         * and the coordinator expires it by token.
         */
        struct Completed
        {
            uint64 socialRequestToken = 0;
            SocialExchangeOutcome outcome = SocialExchangeOutcome::Abandon;
            PlayerbotSocialProviderResult result;
        };

        // World thread: classifies everything that came back since the previous drain.
        std::vector<Completed> Drain();

        /*
         * The decision half of `Drain`, with its two inputs handed in rather than read from the
         * world: the answers to consider, and the instant to judge them at. `Drain` is the thin
         * wrapper that supplies the real ones.
         *
         * Split out for the same reason the rest of this task's rules are pure functions. Every rule
         * here is about time, and driving real time from a test means real sleeps, which buy a
         * multi second suite that still only makes a race unlikely rather than impossible. One clock
         * reading per call is also more correct than three: a sweep, an arrival comparison, and a
         * retry deadline computed from three separate readings can disagree with each other.
         */
        std::vector<Completed> Resolve(std::vector<SocialRawResponse> const& responses, int64 nowMs);

        [[nodiscard]] std::size_t OutstandingCount() const { return _exchanges.size(); }

        // Drops every outstanding exchange. Shutdown, and provider deregistration, both need this:
        // an exchange outlives nothing, and a stale one would match a token the coordinator reissued.
        void Clear() { _exchanges.clear(); }

    private:
        /*
         * The exchange and the request that opened it, together.
         *
         * The request is retained because a regeneration has to re-send the SAME request. Rebuilding
         * it from the world would resolve a second time, and by then the subject may be gone, so the
         * retry would silently become a different question.
         */
        struct Outstanding
        {
            SocialExchange exchange;
            SocialRequest request;

            /*
             * When this exchange stops being worth waiting for.
             *
             * Most ways a request dies are SILENT: it expired in the queue, it could not be
             * serialized, its frame was oversize, the sidecar never answered, the connection dropped,
             * or the answer arrived to a full response queue. None of those produce a payload, so
             * Drain would never see them and the exchange would occupy one of the 512 slots for the
             * rest of the uptime. A deadline is the only thing that can release a request nothing
             * will ever report on.
             */
            int64 expiresAtSteadyMs = 0;
        };

        ClaudeBridge& _bridge;
        std::string _bridgeToken;
        int64 _requestDeadlineMs = 0;
        std::map<uint64, Outstanding> _exchanges;
    };
}

#endif  // MOD_PLAYERBOT_CLAUDE_CLAUDECHAT_H
