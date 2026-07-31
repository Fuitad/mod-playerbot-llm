/*
 * This file is part of the mod-playerbot-claude module.
 */

#ifndef MOD_PLAYERBOT_CLAUDE_CLAUDECHAT_H
#define MOD_PLAYERBOT_CLAUDE_CLAUDECHAT_H

#include "PlayerbotPersonality.h"
#include "PlayerbotCareerPlan.h"

#include <array>
#include <chrono>
#include <condition_variable>
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
    inline constexpr uint32 SCHEMA_VERSION = 2;
    inline constexpr size_t FRAME_HEADER_BYTES = 4;
    inline constexpr size_t MAX_FRAME_PAYLOAD_BYTES = 64 * 1024;
    inline constexpr size_t MAX_RESPONSE_MESSAGE_BYTES = 240;
    inline constexpr size_t MIN_BRIDGE_TOKEN_BYTES = 32;
    inline constexpr uint32 MAX_AMBIENT_MESSAGES_PER_HOUR = 6;
    inline constexpr uint8 AMBIENT_EVENT_KIND = 4;
    inline constexpr uint8 CAREER_EVENT_KIND = 5;
    inline constexpr char AMBIENT_EVENT_MARKER[] = "ambient_world";

    enum class ChatChannel : uint8
    {
        Whisper = 0,
        Party = 1,
        World = 2,
        Career = 3
    };

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
    };

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
    std::string SerializeRequest(ChatRequest const& request, std::string const& token);

    // Strict parser for sidecar responses. Accepts exactly the contract fields with a
    // matching schema version and token, one line of at most MAX_RESPONSE_MESSAGE_BYTES of
    // valid UTF-8, and rejects everything else.
    std::optional<ChatResponse> ParseResponsePayload(std::string const& payload, std::string const& expectedToken);

    struct CareerDecision
    {
        std::string candidateToken;
        PlayerbotRecipeSpendingStyle spendingStyle = PlayerbotRecipeSpendingStyle::None;
    };

    std::string SerializeCareerRequestContent(PlayerbotCareerPlanRequest const& request);
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

        // World thread: returns every completed response since the previous drain.
        std::vector<ChatResponse> DrainResponses();

    private:
        struct Impl;
        std::unique_ptr<Impl> _impl;
    };
}

#endif  // MOD_PLAYERBOT_CLAUDE_CLAUDECHAT_H
