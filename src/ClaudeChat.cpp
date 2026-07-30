/*
 * This file is part of the mod-playerbot-claude module.
 */

#include "ClaudeChat.h"

#include "utf8.h"

#include <boost/asio.hpp>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <map>
#include <thread>

using boost::asio::ip::tcp;

namespace
{
    // Constant-time byte comparison so a token mismatch cannot be timed.
    bool ConstantTimeEquals(std::string const& a, std::string const& b)
    {
        if (a.size() != b.size())
            return false;

        volatile unsigned char acc = 0;
        for (size_t i = 0; i < a.size(); ++i)
            acc = acc | static_cast<unsigned char>(a[i] ^ b[i]);

        return acc == 0;
    }

    void AppendEscapedJsonString(std::string& out, std::string const& text)
    {
        out += '"';
        for (unsigned char c : text)
        {
            switch (c)
            {
                case '"':
                    out += "\\\"";
                    break;
                case '\\':
                    out += "\\\\";
                    break;
                case '\n':
                    out += "\\n";
                    break;
                case '\r':
                    out += "\\r";
                    break;
                case '\t':
                    out += "\\t";
                    break;
                case '\b':
                    out += "\\b";
                    break;
                case '\f':
                    out += "\\f";
                    break;
                default:
                    if (c < 0x20)
                    {
                        char buffer[8];
                        std::snprintf(buffer, sizeof(buffer), "\\u%04x", c);
                        out += buffer;
                    }
                    else
                        out += static_cast<char>(c);
                    break;
            }
        }
        out += '"';
    }

    void AppendJsonField(std::string& out, char const* key, uint64 value, bool first = false)
    {
        if (!first)
            out += ',';
        out += '"';
        out += key;
        out += "\":";
        out += std::to_string(value);
    }

    void AppendJsonField(std::string& out, char const* key, std::string const& value, bool first = false)
    {
        if (!first)
            out += ',';
        out += '"';
        out += key;
        out += "\":";
        AppendEscapedJsonString(out, value);
    }

    std::string ChatChannelName(ClaudeChat::ChatChannel channel)
    {
        switch (channel)
        {
            case ClaudeChat::ChatChannel::Whisper:
                return "whisper";
            case ClaudeChat::ChatChannel::Party:
                return "party";
            case ClaudeChat::ChatChannel::World:
                return "world";
        }

        return "";
    }

    // Strict flat JSON value: unsigned integer or string only.
    struct FlatJsonValue
    {
        bool isString = false;
        uint64 number = 0;
        std::string text;
    };

    // Strict parser for one flat JSON object with unique string keys and values that are
    // either unsigned integers or strings. Anything else (nesting, arrays, booleans,
    // null, floats, negatives, duplicate keys, trailing bytes) fails the parse.
    class FlatJsonParser
    {
    public:
        explicit FlatJsonParser(std::string const& input) : _input(input) { }

        std::optional<std::map<std::string, FlatJsonValue>> Parse()
        {
            std::map<std::string, FlatJsonValue> fields;
            SkipWhitespace();
            if (!Consume('{'))
                return std::nullopt;

            SkipWhitespace();
            if (Consume('}'))
                return Finish(fields);

            while (true)
            {
                SkipWhitespace();
                std::optional<std::string> key = ParseString();
                if (!key)
                    return std::nullopt;

                SkipWhitespace();
                if (!Consume(':'))
                    return std::nullopt;

                SkipWhitespace();
                std::optional<FlatJsonValue> value = ParseValue();
                if (!value)
                    return std::nullopt;

                if (!fields.emplace(*key, std::move(*value)).second)
                    return std::nullopt;  // duplicate key

                SkipWhitespace();
                if (Consume(','))
                    continue;
                if (Consume('}'))
                    return Finish(fields);
                return std::nullopt;
            }
        }

    private:
        std::optional<std::map<std::string, FlatJsonValue>> Finish(std::map<std::string, FlatJsonValue>& fields)
        {
            SkipWhitespace();
            if (_position != _input.size())
                return std::nullopt;  // trailing bytes

            return std::move(fields);
        }

        void SkipWhitespace()
        {
            while (_position < _input.size())
            {
                char const c = _input[_position];
                if (c != ' ' && c != '\t' && c != '\n' && c != '\r')
                    break;
                ++_position;
            }
        }

        bool Consume(char expected)
        {
            if (_position < _input.size() && _input[_position] == expected)
            {
                ++_position;
                return true;
            }
            return false;
        }

        std::optional<FlatJsonValue> ParseValue()
        {
            if (_position >= _input.size())
                return std::nullopt;

            char const c = _input[_position];
            if (c == '"')
            {
                std::optional<std::string> text = ParseString();
                if (!text)
                    return std::nullopt;

                FlatJsonValue value;
                value.isString = true;
                value.text = std::move(*text);
                return value;
            }

            if (c >= '0' && c <= '9')
                return ParseUnsigned();

            return std::nullopt;
        }

        std::optional<FlatJsonValue> ParseUnsigned()
        {
            uint64 result = 0;
            size_t digits = 0;
            while (_position < _input.size() && _input[_position] >= '0' && _input[_position] <= '9')
            {
                uint64 const digit = static_cast<uint64>(_input[_position] - '0');
                if (result > (UINT64_MAX - digit) / 10)
                    return std::nullopt;  // overflow

                result = result * 10 + digit;
                ++_position;
                ++digits;
            }

            if (!digits || digits > 20)
                return std::nullopt;

            // A number followed by '.', 'e', or 'E' would be a float: reject strictly.
            if (_position < _input.size())
            {
                char const next = _input[_position];
                if (next == '.' || next == 'e' || next == 'E')
                    return std::nullopt;
            }

            FlatJsonValue value;
            value.number = result;
            return value;
        }

        std::optional<std::string> ParseString()
        {
            if (!Consume('"'))
                return std::nullopt;

            std::string result;
            while (_position < _input.size())
            {
                unsigned char const c = static_cast<unsigned char>(_input[_position]);
                if (c == '"')
                {
                    ++_position;
                    return result;
                }

                if (c == '\\')
                {
                    ++_position;
                    if (_position >= _input.size())
                        return std::nullopt;

                    char const escape = _input[_position];
                    ++_position;
                    switch (escape)
                    {
                        case '"': result += '"'; break;
                        case '\\': result += '\\'; break;
                        case '/': result += '/'; break;
                        case 'n': result += '\n'; break;
                        case 'r': result += '\r'; break;
                        case 't': result += '\t'; break;
                        case 'b': result += '\b'; break;
                        case 'f': result += '\f'; break;
                        case 'u':
                        {
                            std::optional<uint32> codepoint = ParseHex4();
                            if (!codepoint)
                                return std::nullopt;

                            // Reject surrogates outright: the sidecar emits plain UTF-8.
                            if (*codepoint >= 0xD800 && *codepoint <= 0xDFFF)
                                return std::nullopt;

                            AppendUtf8(result, *codepoint);
                            break;
                        }
                        default:
                            return std::nullopt;
                    }
                    continue;
                }

                if (c < 0x20)
                    return std::nullopt;  // raw control characters are invalid JSON

                result += static_cast<char>(c);
                ++_position;
            }

            return std::nullopt;  // unterminated string
        }

        std::optional<uint32> ParseHex4()
        {
            if (_position + 4 > _input.size())
                return std::nullopt;

            uint32 value = 0;
            for (size_t i = 0; i < 4; ++i)
            {
                char const c = _input[_position + i];
                value <<= 4;
                if (c >= '0' && c <= '9')
                    value |= static_cast<uint32>(c - '0');
                else if (c >= 'a' && c <= 'f')
                    value |= static_cast<uint32>(c - 'a' + 10);
                else if (c >= 'A' && c <= 'F')
                    value |= static_cast<uint32>(c - 'A' + 10);
                else
                    return std::nullopt;
            }

            _position += 4;
            return value;
        }

        static void AppendUtf8(std::string& out, uint32 codepoint)
        {
            if (codepoint < 0x80)
                out += static_cast<char>(codepoint);
            else if (codepoint < 0x800)
            {
                out += static_cast<char>(0xC0 | (codepoint >> 6));
                out += static_cast<char>(0x80 | (codepoint & 0x3F));
            }
            else
            {
                out += static_cast<char>(0xE0 | (codepoint >> 12));
                out += static_cast<char>(0x80 | ((codepoint >> 6) & 0x3F));
                out += static_cast<char>(0x80 | (codepoint & 0x3F));
            }
        }

        std::string const& _input;
        size_t _position = 0;
    };

    bool IsSingleCleanLine(std::string const& text)
    {
        for (unsigned char c : text)
            if (c < 0x20)
                return false;

        return utf8::is_valid(text.begin(), text.end());
    }
}

std::string ClaudeChat::TruncateUtf8Bytes(std::string text, size_t maxBytes)
{
    if (text.size() > maxBytes)
        text.resize(maxBytes);

    // Cut at the first invalid position so a split multibyte sequence (or previously
    // invalid input) can never cross the wire.
    auto const invalid = utf8::find_invalid(text.begin(), text.end());
    text.resize(static_cast<size_t>(invalid - text.begin()));
    return text;
}

int64 ClaudeChat::SteadyNowMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

std::optional<std::string> ClaudeChat::BridgeTokenFromEnvironment()
{
    char const* raw = std::getenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN");
    if (!raw)
        return std::nullopt;

    std::string token(raw);
    if (token.size() < MIN_BRIDGE_TOKEN_BYTES)
        return std::nullopt;

    return token;
}

std::optional<std::vector<uint8>> ClaudeChat::EncodeFrame(std::string const& payload)
{
    if (payload.size() > MAX_FRAME_PAYLOAD_BYTES)
        return std::nullopt;

    uint32 const length = static_cast<uint32>(payload.size());
    std::vector<uint8> frame;
    frame.reserve(FRAME_HEADER_BYTES + payload.size());
    frame.push_back(static_cast<uint8>((length >> 24) & 0xFF));
    frame.push_back(static_cast<uint8>((length >> 16) & 0xFF));
    frame.push_back(static_cast<uint8>((length >> 8) & 0xFF));
    frame.push_back(static_cast<uint8>(length & 0xFF));
    frame.insert(frame.end(), payload.begin(), payload.end());
    return frame;
}

std::optional<uint32> ClaudeChat::DecodeFrameLength(std::array<uint8, FRAME_HEADER_BYTES> const& header)
{
    uint32 const length = (static_cast<uint32>(header[0]) << 24) | (static_cast<uint32>(header[1]) << 16) |
                          (static_cast<uint32>(header[2]) << 8) | static_cast<uint32>(header[3]);
    if (length > MAX_FRAME_PAYLOAD_BYTES)
        return std::nullopt;

    return length;
}

std::string ClaudeChat::SerializeRequest(ChatRequest const& request, std::string const& token)
{
    std::string out;
    out.reserve(512 + request.message.size());
    out += '{';
    AppendJsonField(out, "schema_version", SCHEMA_VERSION, true);
    AppendJsonField(out, "token", token);
    AppendJsonField(out, "request_id", request.requestId);
    AppendJsonField(out, "channel", ChatChannelName(request.channel));
    AppendJsonField(out, "bot_guid", request.botGuidCounter);
    AppendJsonField(out, "speaker_guid", request.speakerGuidCounter);
    AppendJsonField(out, "bot_name", request.botName);
    AppendJsonField(out, "speaker_name", request.speakerName);
    AppendJsonField(out, "profile_version", request.profile.version);
    AppendJsonField(out, "crafting_affinity", request.profile.craftingAffinity);
    AppendJsonField(out, "exploration_affinity", request.profile.explorationAffinity);
    AppendJsonField(out, "sociability", request.profile.sociability);
    AppendJsonField(out, "voice", std::string(PlayerbotPersonality::VoiceName(request.profile.voice)));
    AppendJsonField(out, "event_kind", request.eventKind);
    AppendJsonField(out, "subject_id", request.subjectId);
    AppendJsonField(out, "occurrence", request.occurrence);
    AppendJsonField(out, "message", request.message);
    out += '}';
    return out;
}

std::optional<ClaudeChat::ChatResponse> ClaudeChat::ParseResponsePayload(std::string const& payload,
                                                                        std::string const& expectedToken)
{
    std::optional<std::map<std::string, FlatJsonValue>> fields = FlatJsonParser(payload).Parse();
    if (!fields)
        return std::nullopt;

    if (fields->size() != 4)
        return std::nullopt;

    auto schemaIt = fields->find("schema_version");
    auto tokenIt = fields->find("token");
    auto requestIt = fields->find("request_id");
    auto messageIt = fields->find("message");
    if (schemaIt == fields->end() || tokenIt == fields->end() || requestIt == fields->end() ||
        messageIt == fields->end())
        return std::nullopt;

    if (schemaIt->second.isString || schemaIt->second.number != SCHEMA_VERSION)
        return std::nullopt;

    if (!tokenIt->second.isString || !ConstantTimeEquals(tokenIt->second.text, expectedToken))
        return std::nullopt;

    if (requestIt->second.isString)
        return std::nullopt;

    if (!messageIt->second.isString)
        return std::nullopt;

    std::string const& message = messageIt->second.text;
    if (message.empty() || message.size() > MAX_RESPONSE_MESSAGE_BYTES || !IsSingleCleanLine(message))
        return std::nullopt;

    ChatResponse response;
    response.requestId = requestIt->second.number;
    response.message = message;
    return response;
}

// --- Milestone speaker selection ---

namespace
{
    constexpr uint64 MILESTONE_NAMESPACE = 0x4D494C4553544F4EULL;
    constexpr uint64 AMBIENT_NAMESPACE = 0x414D4249454E5400ULL;
}

std::optional<uint64> ClaudeChat::SelectMilestoneSpeaker(MilestoneEventId const& eventId,
                                                         std::vector<SpeakerCandidate> candidates)
{
    if (candidates.empty())
        return std::nullopt;

    std::sort(candidates.begin(), candidates.end(),
              [](SpeakerCandidate const& a, SpeakerCandidate const& b) { return a.guidCounter < b.guidCounter; });

    uint64 seed = PlayerbotPersonality::SplitMix64(eventId.actorGuidCounter ^ MILESTONE_NAMESPACE ^
                                                   static_cast<uint64>(MILESTONE_SELECTION_VERSION));
    seed = PlayerbotPersonality::SplitMix64(seed ^ static_cast<uint64>(eventId.kind));
    seed = PlayerbotPersonality::SplitMix64(seed ^ eventId.subjectId);
    seed = PlayerbotPersonality::SplitMix64(seed ^ eventId.occurrence);
    for (SpeakerCandidate const& candidate : candidates)
        seed = PlayerbotPersonality::SplitMix64(seed ^ candidate.guidCounter);

    uint64 totalWeight = 0;
    for (SpeakerCandidate const& candidate : candidates)
        totalWeight += 1u + candidate.sociability;

    uint64 const roll = seed % totalWeight;
    uint64 cumulative = 0;
    for (SpeakerCandidate const& candidate : candidates)
    {
        cumulative += 1u + candidate.sociability;
        if (cumulative > roll)
            return candidate.guidCounter;
    }

    return candidates.back().guidCounter;
}

std::optional<uint64> ClaudeChat::SelectAmbientSpeaker(uint64 occurrence,
                                                       std::vector<SpeakerCandidate> candidates)
{
    if (candidates.empty())
        return std::nullopt;

    std::sort(candidates.begin(), candidates.end(),
              [](SpeakerCandidate const& a, SpeakerCandidate const& b) { return a.guidCounter < b.guidCounter; });

    uint64 seed = PlayerbotPersonality::SplitMix64(occurrence ^ AMBIENT_NAMESPACE ^
                                                   static_cast<uint64>(MILESTONE_SELECTION_VERSION));
    for (SpeakerCandidate const& candidate : candidates)
        seed = PlayerbotPersonality::SplitMix64(seed ^ candidate.guidCounter);

    uint64 totalWeight = 0;
    for (SpeakerCandidate const& candidate : candidates)
        totalWeight += 1u + candidate.sociability;

    uint64 const roll = seed % totalWeight;
    uint64 cumulative = 0;
    for (SpeakerCandidate const& candidate : candidates)
    {
        cumulative += 1u + candidate.sociability;
        if (cumulative > roll)
            return candidate.guidCounter;
    }

    return candidates.back().guidCounter;
}

ClaudeChat::AmbientCadence::AmbientCadence(uint32 messagesPerHour, int64 startMs)
{
    if (!messagesPerHour || messagesPerHour > MAX_AMBIENT_MESSAGES_PER_HOUR)
        return;

    _intervalMs = 60 * 60 * 1000 / messagesPerHour;
    _nextDueMs = startMs + _intervalMs;
}

bool ClaudeChat::AmbientCadence::IsValid() const
{
    return _intervalMs > 0;
}

bool ClaudeChat::AmbientCadence::TryConsumeDueSlot(int64 nowMs)
{
    if (!IsValid() || nowMs < _nextDueMs)
        return false;

    _nextDueMs = nowMs + _intervalMs;
    return true;
}

bool ClaudeChat::ShouldEnqueueAmbient(bool humanOnline,
                                      std::vector<AmbientCandidateSnapshot> const& candidates)
{
    if (!humanOnline)
        return false;

    return std::any_of(candidates.begin(), candidates.end(), [](AmbientCandidateSnapshot const& candidate)
    {
        return candidate.botOnline && candidate.botAlive && candidate.botIsMachine && !candidate.botInCombat &&
               candidate.worldChannelAvailable;
    });
}

bool ClaudeChat::RecentEventIdSet::Insert(MilestoneEventId const& eventId)
{
    for (MilestoneEventId const& seen : _order)
        if (seen == eventId)
            return false;

    _order.push_back(eventId);
    if (_order.size() > _capacity)
        _order.pop_front();

    return true;
}

// --- Explicit chat capture ---

std::optional<std::string> ClaudeChat::WhisperClaudeText(std::string const& message,
                                                         bool isKnownPlayerbotCommand)
{
    // An explicit llm attempt always wins (even for command-shaped text), and a
    // malformed one ("llm", "llm ") stays silent rather than leaking the word "llm".
    if (message == "llm" || message.rfind("llm ", 0) == 0)
        return ParseLlmWhisper(message);

    if (isKnownPlayerbotCommand)
        return std::nullopt;

    // Whitespace-only text never costs tokens.
    if (message.find_first_not_of(" \t") == std::string::npos)
        return std::nullopt;

    return message;
}

std::optional<std::string> ClaudeChat::ParseLlmWhisper(std::string const& message)
{
    static constexpr char PREFIX[] = "llm ";
    if (message.rfind(PREFIX, 0) != 0)
        return std::nullopt;

    std::string text = message.substr(sizeof(PREFIX) - 1);
    if (text.empty() || text.find_first_not_of(' ') == std::string::npos)
        return std::nullopt;

    return text;
}

std::optional<std::pair<std::string, std::string>> ClaudeChat::ParseLlmParty(std::string const& message)
{
    std::optional<std::string> const remainder = ParseLlmWhisper(message);
    if (!remainder)
        return std::nullopt;

    size_t const space = remainder->find(' ');
    if (space == std::string::npos)
        return std::nullopt;

    std::string name = remainder->substr(0, space);
    std::string text = remainder->substr(space + 1);
    if (name.empty() || text.empty() || text.find_first_not_of(' ') == std::string::npos)
        return std::nullopt;

    return std::make_pair(std::move(name), std::move(text));
}

// --- Delivery policy ---

bool ClaudeChat::ShouldDeliver(ChatChannel channel, DeliverySnapshot const& snapshot)
{
    if (snapshot.expired)
        return false;

    if (!snapshot.botOnline || !snapshot.botIsStillBot)
        return false;

    if (channel == ChatChannel::World)
        return snapshot.botAlive && !snapshot.botInCombat && snapshot.humanOnline && snapshot.worldChannelAvailable;

    if (!snapshot.speakerOnline)
        return false;

    // Whispering while fighting is normal play, so combat only mutes the noisier
    // party milestone reactions.
    if (channel == ChatChannel::Party && (snapshot.botInCombat || !snapshot.sameGroup))
        return false;

    return true;
}

bool ClaudeChat::GroupCooldownTracker::TryBegin(uint64 groupId, int64 nowMs, int64 cooldownMs)
{
    auto it = _lastBeginMs.find(groupId);
    if (it != _lastBeginMs.end() && nowMs - it->second < cooldownMs)
        return false;

    _lastBeginMs[groupId] = nowMs;
    return true;
}

// --- Bridge worker ---

struct ClaudeChat::ClaudeBridge::Impl
{
    explicit Impl(BridgeConfig bridgeConfig)
        : config(std::move(bridgeConfig)), requests(config.queueCapacity), responses(config.queueCapacity)
    {
    }

    BridgeConfig config;
    BoundedQueue<ChatRequest> requests;
    BoundedQueue<ChatResponse> responses;
    std::atomic<bool> started{false};
    std::atomic<bool> stopped{false};

    boost::asio::io_context io;
    std::mutex socketMutex;
    std::optional<tcp::socket> socket;
    std::jthread worker;

    // Stop-side abort: shuts the socket down so a blocked worker read returns
    // immediately. Never destroys the socket object; only the worker does that
    // (DiscardSocket), so a concurrently blocked read never touches freed memory.
    void AbortSocket()
    {
        std::lock_guard<std::mutex> lock(socketMutex);
        if (socket)
        {
            boost::system::error_code ec;
            socket->shutdown(tcp::socket::shutdown_both, ec);
            socket->close(ec);
        }
    }

    // Worker-only: drop the (possibly closed) socket object.
    void DiscardSocket()
    {
        std::lock_guard<std::mutex> lock(socketMutex);
        if (socket)
        {
            boost::system::error_code ec;
            socket->close(ec);
            socket.reset();
        }
    }

    bool EnsureConnected()
    {
        if (stopped.load())
            return false;

        {
            std::lock_guard<std::mutex> lock(socketMutex);
            if (socket && socket->is_open())
                return true;
        }

        DiscardSocket();

        tcp::socket candidate(io);
        boost::system::error_code ec;
        candidate.connect(tcp::endpoint(boost::asio::ip::make_address(config.host), config.port), ec);
        if (ec)
            return false;

        std::lock_guard<std::mutex> lock(socketMutex);
        socket.emplace(std::move(candidate));
        if (stopped.load())
        {
            // Stop ran while connecting and may have missed this socket: close it now so
            // the worker cannot enter a long read after shutdown began.
            boost::system::error_code closeEc;
            socket->close(closeEc);
            return false;
        }
        return true;
    }

    void SetReadTimeout(int64 timeoutMs)
    {
        std::lock_guard<std::mutex> lock(socketMutex);
        if (!socket)
            return;

        timeval tv{};
        tv.tv_sec = static_cast<decltype(tv.tv_sec)>(timeoutMs / 1000);
        tv.tv_usec = static_cast<decltype(tv.tv_usec)>((timeoutMs % 1000) * 1000);
        setsockopt(socket->native_handle(), SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    }

    bool SendFrame(std::vector<uint8> const& frame)
    {
        boost::system::error_code ec;
        std::lock_guard<std::mutex> lock(socketMutex);
        if (!socket)
            return false;

        boost::asio::write(*socket, boost::asio::buffer(frame), ec);
        return !ec;
    }

    // Blocking read of one response frame. The socket read timeout and a stop-side
    // CloseSocket both abort it promptly.
    std::optional<std::string> ReadFrame()
    {
        std::array<uint8, FRAME_HEADER_BYTES> header{};
        boost::system::error_code ec;

        {
            std::lock_guard<std::mutex> lock(socketMutex);
            if (!socket || stopped.load())
                return std::nullopt;
        }

        // Reads intentionally run without the mutex so Stop can shut the socket down
        // concurrently (AbortSocket). The socket object itself is only ever destroyed by
        // the worker (DiscardSocket), so this read never touches freed memory.
        //
        // Do NOT "fix" this by holding socketMutex across the read: AbortSocket takes the
        // same mutex, so a blocked read would deadlock shutdown. The concurrency here is
        // the POSIX guarantee that shutdown()/close() on a socket fd wakes a thread
        // blocked in a synchronous read on that fd with an error; Asio's basic_socket
        // does not promise general concurrent-call safety, which is why the ONLY
        // concurrent calls ever made are AbortSocket's shutdown+close.
        boost::asio::read(*socket, boost::asio::buffer(header), ec);
        if (ec)
            return std::nullopt;

        std::optional<uint32> const length = DecodeFrameLength(header);
        if (!length)
            return std::nullopt;

        std::string payload(*length, '\0');
        if (*length)
        {
            boost::asio::read(*socket, boost::asio::buffer(payload.data(), payload.size()), ec);
            if (ec)
                return std::nullopt;
        }

        return payload;
    }

    void Run(std::stop_token stopToken)
    {
        while (!stopToken.stop_requested())
        {
            ChatRequest request;
            if (!requests.WaitPop(request, std::chrono::milliseconds(200)))
                continue;

            if (SteadyNowMs() > request.expiresAtSteadyMs)
                continue;  // expired in queue: discard, fail closed

            std::optional<std::vector<uint8>> const frame =
                EncodeFrame(SerializeRequest(request, config.token));
            if (!frame)
                continue;  // oversized request: drop

            while (!stopToken.stop_requested() && SteadyNowMs() <= request.expiresAtSteadyMs)
            {
                if (!EnsureConnected())
                {
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                    continue;
                }

                if (!SendFrame(*frame))
                {
                    DiscardSocket();
                    continue;  // reconnect and retry until the request expires
                }

                int64 const remainingMs = request.expiresAtSteadyMs - SteadyNowMs();
                SetReadTimeout(std::max<int64>(remainingMs, 100));

                std::optional<std::string> const payload = ReadFrame();
                if (!payload)
                {
                    // Timeout, closed connection, or oversized frame: the request is
                    // lost. Never fabricate a response.
                    DiscardSocket();
                    break;
                }

                if (std::optional<ChatResponse> response = ParseResponsePayload(*payload, config.token))
                    responses.TryPush(std::move(*response));

                break;
            }
        }

        DiscardSocket();
    }
};

ClaudeChat::ClaudeBridge::ClaudeBridge(BridgeConfig config) : _impl(std::make_unique<Impl>(std::move(config)))
{
}

ClaudeChat::ClaudeBridge::~ClaudeBridge()
{
    Stop();
}

void ClaudeChat::ClaudeBridge::Start()
{
    if (_impl->started.exchange(true))
        return;

    _impl->worker = std::jthread([impl = _impl.get()](std::stop_token stopToken) { impl->Run(stopToken); });
}

void ClaudeChat::ClaudeBridge::Stop()
{
    if (_impl->stopped.exchange(true))
        return;

    if (_impl->worker.joinable())
        _impl->worker.request_stop();

    _impl->requests.Stop();
    _impl->responses.Stop();
    _impl->AbortSocket();

    if (_impl->worker.joinable())
        _impl->worker.join();
}

bool ClaudeChat::ClaudeBridge::TryEnqueue(ChatRequest request)
{
    if (_impl->stopped.load())
        return false;

    if (SteadyNowMs() > request.expiresAtSteadyMs)
        return false;

    return _impl->requests.TryPush(std::move(request));
}

std::vector<ClaudeChat::ChatResponse> ClaudeChat::ClaudeBridge::DrainResponses()
{
    std::vector<ChatResponse> drained;
    ChatResponse response;
    while (_impl->responses.TryPop(response))
        drained.push_back(std::move(response));

    return drained;
}
