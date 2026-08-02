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
#include <variant>

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
            case ClaudeChat::ChatChannel::Career:
                return "career";
            case ClaudeChat::ChatChannel::Social:
                return "social";
            default:
                break;
        }

        return "";
    }

    std::string SpendingStyleName(PlayerbotRecipeSpendingStyle style)
    {
        switch (style)
        {
            case PlayerbotRecipeSpendingStyle::None:
                return "none";
            case PlayerbotRecipeSpendingStyle::Minimal:
                return "minimal";
            case PlayerbotRecipeSpendingStyle::Progression:
                return "progression";
            case PlayerbotRecipeSpendingStyle::Completionist:
                return "completionist";
        }

        return "";
    }

    std::optional<PlayerbotRecipeSpendingStyle> ParseSpendingStyle(std::string const& style)
    {
        if (style == "none")
            return PlayerbotRecipeSpendingStyle::None;
        if (style == "minimal")
            return PlayerbotRecipeSpendingStyle::Minimal;
        if (style == "progression")
            return PlayerbotRecipeSpendingStyle::Progression;
        if (style == "completionist")
            return PlayerbotRecipeSpendingStyle::Completionist;
        return std::nullopt;
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

    // Both bounds at the one place the token enters the process, so nothing downstream has to be
    // the last thing standing between a misconfigured environment and every frame it would sign.
    if (!BridgeTokenIsUsable(token))
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

std::optional<std::string> ClaudeChat::SerializeRequest(ChatRequest const& request, std::string const& token)
{
    /*
     * Every string is checked here as well as in the sidecar. Relying on the far side means an
     * oversize frame is built, sent, and rejected, and the caller learns nothing about which request
     * caused it. std::string::size() is a byte count in C++, so these are the same budgets rather
     * than a looser character version of them.
     */
    if (!BridgeTokenIsUsable(token))
        return std::nullopt;

    if (request.botName.empty() || request.botName.size() > MAX_ACTOR_NAME_BYTES)
        return std::nullopt;

    if (request.speakerName.size() > MAX_ACTOR_NAME_BYTES)
        return std::nullopt;

    // A career payload is a bounded nested document rather than one remark, so it has its own,
    // much larger budget. Everything else is held to the conversational one.
    size_t const messageBudget =
        request.channel == ChatChannel::Career ? MAX_CAREER_MESSAGE_BYTES : MAX_REQUEST_MESSAGE_BYTES;
    if (request.message.empty() || request.message.size() > messageBudget)
        return std::nullopt;

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
    AppendJsonField(out, "gathering_affinity", request.profile.gatheringAffinity);
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

    // Explicit rather than incidental. Equality against a validated token and the frame ceiling
    // bounded this only as a side effect, and the rule this protocol claims is that no string is
    // bounded as a side effect.
    if (!BridgeTokenIsUsable(expectedToken))
        return std::nullopt;

    if (!tokenIt->second.isString || !BridgeTokenIsUsable(tokenIt->second.text) ||
        !ConstantTimeEquals(tokenIt->second.text, expectedToken))
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

bool ClaudeChat::ResponseKindIsValid(ResponseKind kind)
{
    switch (kind)
    {
        case ResponseKind::Chat:
        case ResponseKind::Career:
        case ResponseKind::Social:
            return true;
        default:
            break;
    }

    return false;
}

char const* ClaudeChat::ResponseKindName(ResponseKind kind)
{
    switch (kind)
    {
        case ResponseKind::Chat:
            return "chat";
        case ResponseKind::Career:
            return "career";
        case ResponseKind::Social:
            return "social";
        default:
            break;
    }

    return "unknown";
}

std::optional<ClaudeChat::ResponseKind> ClaudeChat::ResponseKindFromName(std::string const& name)
{
    // Exact match only. A prefix or case insensitive match would let "social_draft" or "Career" be
    // accepted as something the sender did not say.
    if (name == "chat")
        return ResponseKind::Chat;
    if (name == "career")
        return ResponseKind::Career;
    if (name == "social")
        return ResponseKind::Social;

    return std::nullopt;
}

bool ClaudeChat::BridgeTokenIsUsable(std::string const& token)
{
    return token.size() >= MIN_BRIDGE_TOKEN_BYTES && token.size() <= MAX_BRIDGE_TOKEN_BYTES;
}

bool ClaudeChat::ActorIsAbsent(Actor const& actor)
{
    // Every field, not just the guid. A zero guid with a name attached is an orphan that still
    // travels and still describes somebody who is not there.
    return actor.guidCounter == 0 && actor.name.empty() && !actor.human;
}

bool ClaudeChat::ActorIsUsable(Actor const& actor)
{
    // A nameless or unnamed actor cannot describe a real character, and an unbounded name is not a
    // bounded frame. Both are refused before anything is serialized.
    if (actor.guidCounter == 0)
        return false;

    if (actor.name.empty() || actor.name.size() > MAX_ACTOR_NAME_BYTES)
        return false;

    /*
     * Validity BEFORE the character count, and the order is load bearing rather than tidy.
     *
     * `utf8::distance` is the checked variant: it walks the string with `utf8::next`, which
     * THROWS `utf8::invalid_utf8` on a malformed sequence. This runs on the world thread while a
     * request is being assembled, so counting first would turn a malformed name into an exception
     * escaping into the caller, which is a far worse outcome than the overlong name the count
     * exists to refuse.
     */
    if (!IsSingleCleanLine(actor.name))
        return false;

    // Characters as well as bytes. Counted as UTF-8 code points rather than as bytes, because
    // twelve characters of a multibyte script is a legitimate name that a byte count would refuse.
    return utf8::distance(actor.name.begin(), actor.name.end()) <= MAX_ACTOR_NAME_CHARACTERS;
}

bool ClaudeChat::SocialExchangeOutcomeIsValid(SocialExchangeOutcome outcome)
{
    switch (outcome)
    {
        case SocialExchangeOutcome::Deliver:
        case SocialExchangeOutcome::Regenerate:
        case SocialExchangeOutcome::Abandon:
            return true;
        default:
            break;
    }

    return false;
}

ClaudeChat::SocialExchangeOutcome ClaudeChat::SocialExchange::Classify(std::string const& payload,
                                                                       std::string const& expectedToken,
                                                                       SocialResponse& out)
{
    /*
     * The parser already refuses a mismatched schema, token, kind, request identity, and bot
     * identity, so anything it rejects is abandoned here without further inspection. That is the
     * fail closed path for a missing sidecar, a protocol mismatch, and an invalid response alike:
     * all three arrive as "this did not parse".
     */
    std::optional<SocialResponse> const parsed =
        ParseSocialResponsePayload(payload, expectedToken, _socialRequestToken, _botGuidCounter);
    if (!parsed)
        return SocialExchangeOutcome::Abandon;

    if (parsed->regenerate)
    {
        /*
         * At most MAX_REGENERATIONS_PER_REQUEST. A sidecar that keeps reporting its own output
         * unusable would otherwise be retried forever on one request, and the coordinator, not this
         * class, decides whether a second REQUEST is worth making at all.
         */
        if (_regenerations >= MAX_REGENERATIONS_PER_REQUEST)
            return SocialExchangeOutcome::Abandon;

        ++_regenerations;
        return SocialExchangeOutcome::Regenerate;
    }

    out = *parsed;
    return SocialExchangeOutcome::Deliver;
}

bool ClaudeChat::ClaudeSocialTransport::Submit(SocialRequest const& request)
{
    return SubmitAt(request, SteadyNowMs());
}

bool ClaudeChat::ClaudeSocialTransport::SubmitAt(SocialRequest const& request, int64 nowMs)
{
    /*
     * Every refusal here is immediate and final, which is the point. The coordinator reads a false
     * as ProviderFailed and produces silence now, rather than holding the bot's slot until its own
     * thirty second timeout expires a request that was never going to be answered.
     */
    if (!SocialRequestIsUsable(request, _bridgeToken))
        return false;

    // Never replace the exchange that already owns this token. The coordinator does not reuse one,
    // so a second submission under the same token is a caller bug, and silently dropping the first
    // exchange would leave its answer unmatchable.
    if (_exchanges.contains(request.socialRequestToken))
        return false;

    /*
     * The same ceiling the coordinator bounds its pending deliveries with. Without it a provider
     * whose drain is never called accumulates one retained request per token for the rest of the
     * uptime, and the retained copy is the whole request including its context.
     */
    if (_exchanges.size() >= MAX_OUTSTANDING_SOCIAL_REQUESTS)
        return false;

    int64 const expiresAtSteadyMs = nowMs + _requestDeadlineMs;
    if (!_bridge.TryEnqueueSocial(request, expiresAtSteadyMs))
        return false;

    _exchanges.emplace(request.socialRequestToken,
                       Outstanding{SocialExchange(request.socialRequestToken, request.bot.guidCounter), request,
                                   expiresAtSteadyMs});
    return true;
}

std::vector<ClaudeChat::ClaudeSocialTransport::Completed> ClaudeChat::ClaudeSocialTransport::Drain()
{
    return Resolve(_bridge.DrainSocialResponses(), SteadyNowMs());
}

std::vector<ClaudeChat::ClaudeSocialTransport::Completed> ClaudeChat::ClaudeSocialTransport::Resolve(
    std::vector<SocialRawResponse> const& responses, int64 nowMs)
{
    std::vector<Completed> completed;

    /*
     * Answers are read BEFORE the sweep below, and each is judged by when it ARRIVED rather than by
     * when this drain happens to run. Sweeping first would erase an exchange whose valid answer was
     * already sitting in the queue, purely because the world thread looked one tick late. Draining
     * first without checking arrival would do the opposite and accept an answer that missed its
     * deadline. The stamp the worker takes on arrival is what separates the two cases.
     */
    for (SocialRawResponse const& raw : responses)
    {
        auto const outstanding = _exchanges.find(raw.socialRequestToken);
        if (outstanding == _exchanges.end())
            continue;  // Cleared while its answer was in flight. Never resurrect an exchange.

        if (raw.receivedAtSteadyMs > outstanding->second.expiresAtSteadyMs)
        {
            // Late. The coordinator has its own timeout and may already have given up, so treating
            // this as an answer would deliver a line for a conversation that has moved on.
            _exchanges.erase(outstanding);
            completed.push_back(Completed{raw.socialRequestToken, SocialExchangeOutcome::Abandon, {}});
            continue;
        }

        SocialResponse response;
        SocialExchangeOutcome const outcome = outstanding->second.exchange.Classify(raw.payload, _bridgeToken,
                                                                                   response);

        if (outcome == SocialExchangeOutcome::Regenerate)
        {
            /*
             * Re-send the request that is already retained rather than rebuilding it. Rebuilding
             * would resolve the world a second time, and by then the subject may be gone, so the
             * retry would quietly become a different question.
             *
             * A regeneration the bridge will not take is the end of this exchange: the budget has
             * already been spent, so there is no second retry to fall back on.
             */
            int64 const retryExpiresAtSteadyMs = nowMs + _requestDeadlineMs;
            if (_bridge.TryEnqueueSocial(outstanding->second.request, retryExpiresAtSteadyMs))
            {
                // The exchange's own deadline moves with the retry. Leaving it on the original would
                // let the sweep above drop the exchange while its regeneration was still in flight,
                // and the answer would then arrive for a token nobody is waiting on.
                outstanding->second.expiresAtSteadyMs = retryExpiresAtSteadyMs;
                completed.push_back(Completed{raw.socialRequestToken, SocialExchangeOutcome::Regenerate, {}});
                continue;
            }

            _exchanges.erase(outstanding);
            completed.push_back(Completed{raw.socialRequestToken, SocialExchangeOutcome::Abandon, {}});
            continue;
        }

        if (outcome != SocialExchangeOutcome::Deliver)
        {
            _exchanges.erase(outstanding);
            completed.push_back(Completed{raw.socialRequestToken, SocialExchangeOutcome::Abandon, {}});
            continue;
        }

        /*
         * The channel travels as the SIDECAR reported it, not as the coordinator asked for it.
         * Substituting the requested channel would make the coordinator's ChannelSwitch refusal
         * unreachable, and that refusal is what keeps a party remark out of a zone channel.
         *
         * A value outside the enum is abandoned rather than cast: this build has neither -Wswitch
         * nor -Werror, so a cast one reaches a consumer unchallenged.
         */
        auto const channel = static_cast<PlayerbotSocialChannel>(response.speakOnChannel);
        if (!PlayerbotSocialChannelIsValid(channel))
        {
            _exchanges.erase(outstanding);
            completed.push_back(Completed{raw.socialRequestToken, SocialExchangeOutcome::Abandon, {}});
            continue;
        }

        PlayerbotSocialProviderResult result;
        result.requestToken = raw.socialRequestToken;
        /*
         * A Message or an Emote, and never a Silence.
         *
         * The parser has already refused an answer carrying both and one carrying neither, so the
         * branch below is a read of which one arrived rather than a decision. Silence stays
         * unreachable: schema 3 has no way to say "chose not to speak", and an empty answer is
         * refused as malformed rather than quietly becoming one. That gap belongs to the response
         * models, not here.
         */
        if (response.emoteId != 0)
        {
            result.kind = PlayerbotSocialOutputKind::Emote;
            result.emoteId = response.emoteId;
        }
        else
        {
            result.kind = PlayerbotSocialOutputKind::Message;
            result.text = response.message;
        }
        result.channel = channel;

        // Consumed either way: a result is delivered once or not at all, never retried into a
        // conversation that has already moved on.
        _exchanges.erase(outstanding);
        completed.push_back(Completed{raw.socialRequestToken, SocialExchangeOutcome::Deliver, std::move(result)});
    }

    /*
     * Whatever is left never answered at all. Most ways a request dies are silent: an expired queue
     * entry, a request that could not be serialized, an oversize frame, a sidecar that never
     * replied, a dropped connection, and an answer that arrived to a full response queue all
     * produce no payload. Without this sweep those exchanges hold their slots forever, and 512 of
     * them is a transport that refuses every subsequent request for the rest of the uptime.
     */
    for (auto entry = _exchanges.begin(); entry != _exchanges.end();)
    {
        if (entry->second.expiresAtSteadyMs > nowMs)
        {
            ++entry;
            continue;
        }

        completed.push_back(Completed{entry->first, SocialExchangeOutcome::Abandon, {}});
        entry = _exchanges.erase(entry);
    }

    return completed;
}

bool ClaudeChat::SocialEmoteIsSupported(uint32 emoteId)
{
    return std::find(SOCIAL_EMOTE_IDS.begin(), SOCIAL_EMOTE_IDS.end(), emoteId) != SOCIAL_EMOTE_IDS.end();
}

int64 ClaudeChat::SocialRequestDeadlineMs(int64 configuredDeadlineMs)
{
    return std::min<int64>(configuredDeadlineMs,
                           static_cast<int64>(PLAYERBOT_SOCIAL_PROVIDER_TIMEOUT_SECONDS) * 1000);
}

bool ClaudeChat::SocialRequestIsUsable(SocialRequest const& request, std::string const& token)
{
    /*
     * Refused before anything is written. The sidecar enforces these too, but a bound checked only
     * on the far side means an oversize frame is built, sent, and rejected, and the caller learns
     * nothing about which request was at fault.
     */
    if (!BridgeTokenIsUsable(token))
        return false;

    if (request.socialRequestToken == 0 || !ActorIsUsable(request.bot))
        return false;

    /*
     * The subject is either fully absent or fully usable, never half described. Accepting a zero
     * guid with a name still attached would serialize a participant nothing can resolve, and a
     * prompt builder reading that name would describe somebody who is not there.
     */
    if (!ActorIsAbsent(request.subject) && !ActorIsUsable(request.subject))
        return false;

    if (request.threadPublicId.empty() || request.threadPublicId.size() > MAX_THREAD_ID_BYTES)
        return false;

    if (request.context.size() > MAX_SOCIAL_CONTEXT_BYTES)
        return false;

    return true;
}

std::optional<std::string> ClaudeChat::SerializeSocialRequest(SocialRequest const& request,
                                                               std::string const& token)
{
    if (!SocialRequestIsUsable(request, token))
        return std::nullopt;

    /*
     * The bot and the subject are written through the same field shape, differing only in the
     * `human` flag. Two shapes would let a prompt builder treat them differently by accident, and
     * the contract is explicit that a human's priority comes from being actively engaged rather than
     * from being human.
     */
    std::string out;
    out.reserve(512 + request.context.size());
    out += '{';
    AppendJsonField(out, "schema_version", SCHEMA_VERSION, true);
    AppendJsonField(out, "token", token);
    AppendJsonField(out, "kind", std::string(ResponseKindName(ResponseKind::Social)));
    AppendJsonField(out, "social_request_token", request.socialRequestToken);
    AppendJsonField(out, "bot_guid", request.bot.guidCounter);
    AppendJsonField(out, "bot_name", request.bot.name);
    AppendJsonField(out, "bot_human", request.bot.human ? uint64{1} : uint64{0});
    AppendJsonField(out, "subject_guid", request.subject.guidCounter);
    AppendJsonField(out, "subject_name", request.subject.name);
    AppendJsonField(out, "subject_human", request.subject.human ? uint64{1} : uint64{0});
    AppendJsonField(out, "speak_on_channel", static_cast<uint64>(request.speakOnChannel));
    AppendJsonField(out, "thread_id", request.threadPublicId);
    AppendJsonField(out, "context", request.context);
    out += '}';
    return out;
}

std::optional<ClaudeChat::SocialResponse> ClaudeChat::ParseSocialResponsePayload(std::string const& payload,
                                                                                 std::string const& expectedToken,
                                                                                 uint64 expectedRequestToken,
                                                                                 uint64 expectedBotGuidCounter)
{
    std::optional<std::map<std::string, FlatJsonValue>> fields = FlatJsonParser(payload).Parse();
    if (!fields)
        return std::nullopt;

    // Exactly the contract fields. The parser already refuses duplicate keys, and an exact count
    // refuses unknown ones, so nothing can ride along unnoticed.
    if (fields->size() != 9)
        return std::nullopt;

    auto const schemaIt = fields->find("schema_version");
    auto const tokenIt = fields->find("token");
    auto const kindIt = fields->find("kind");
    auto const requestIt = fields->find("social_request_token");
    auto const botIt = fields->find("bot_guid");
    auto const channelIt = fields->find("speak_on_channel");
    auto const messageIt = fields->find("message");
    auto const emoteIt = fields->find("emote_id");
    auto const regenerateIt = fields->find("regenerate");

    if (schemaIt == fields->end() || tokenIt == fields->end() || kindIt == fields->end() ||
        requestIt == fields->end() || botIt == fields->end() || channelIt == fields->end() ||
        messageIt == fields->end() || emoteIt == fields->end() || regenerateIt == fields->end())
        return std::nullopt;

    if (schemaIt->second.isString || schemaIt->second.number != SCHEMA_VERSION)
        return std::nullopt;

    if (!BridgeTokenIsUsable(expectedToken))
        return std::nullopt;

    if (!tokenIt->second.isString || !BridgeTokenIsUsable(tokenIt->second.text) ||
        !ConstantTimeEquals(tokenIt->second.text, expectedToken))
        return std::nullopt;

    /*
     * The kind is checked before anything is read out of the payload. Career decisions and social
     * lines travel the same socket, and telling them apart by shape rather than by declaration is
     * how a crafting choice ends up spoken in a zone channel.
     */
    if (!kindIt->second.isString)
        return std::nullopt;

    std::optional<ResponseKind> const kind = ResponseKindFromName(kindIt->second.text);
    if (!kind || *kind != ResponseKind::Social)
        return std::nullopt;

    /*
     * Identity, before content. A perfectly well formed answer to a DIFFERENT request, or for a
     * different bot, is refused rather than delivered to whoever is waiting now.
     */
    if (requestIt->second.isString || requestIt->second.number != expectedRequestToken)
        return std::nullopt;

    if (botIt->second.isString || botIt->second.number != expectedBotGuidCounter)
        return std::nullopt;

    if (channelIt->second.isString || channelIt->second.number > UINT8_MAX)
        return std::nullopt;

    if (regenerateIt->second.isString || regenerateIt->second.number > 1)
        return std::nullopt;

    if (emoteIt->second.isString || emoteIt->second.number > UINT32_MAX)
        return std::nullopt;

    if (!messageIt->second.isString)
        return std::nullopt;

    SocialResponse response;
    response.socialRequestToken = requestIt->second.number;
    response.botGuidCounter = botIt->second.number;
    response.speakOnChannel = static_cast<uint8>(channelIt->second.number);
    response.regenerate = regenerateIt->second.number == 1;
    response.message = messageIt->second.text;
    response.emoteId = static_cast<uint32>(emoteIt->second.number);

    /*
     * A regeneration request is the sidecar saying its own output was unusable, so it is allowed to
     * carry neither a line nor a gesture. Anything claiming to be deliverable is not.
     */
    if (response.regenerate)
        return response;

    /*
     * Exactly one answer. Both is the sidecar hedging, and choosing one here would be inventing an
     * intention it did not express; the coordinator would drop the text anyway, so a frame carrying
     * both is refused where the caller can still be told which request was at fault.
     *
     * Neither is not silence either. Schema 3 has no way to say "chose not to speak", so an empty
     * answer is a malformed one.
     */
    if (response.emoteId != 0)
    {
        if (!response.message.empty())
            return std::nullopt;

        // The allowlist, enforced where the value is read rather than only where it was chosen.
        if (!SocialEmoteIsSupported(response.emoteId))
            return std::nullopt;

        return response;
    }

    if (response.message.empty() || response.message.size() > MAX_RESPONSE_MESSAGE_BYTES ||
        !IsSingleCleanLine(response.message))
        return std::nullopt;

    return response;
}

std::optional<std::string> ClaudeChat::SerializeCareerRequestContent(PlayerbotCareerPlanRequest const& request)
{
    // Refused whole rather than emitted with one bad candidate in it, so a single oversize summary
    // cannot make the entire legal set unusable on the far side.
    for (PlayerbotCareerCandidateView const& candidate : request.candidates)
    {
        if (candidate.token.empty() || candidate.token.size() > MAX_CAREER_TOKEN_BYTES)
            return std::nullopt;

        if (candidate.summary.empty() || candidate.summary.size() > MAX_CAREER_SUMMARY_BYTES)
            return std::nullopt;
    }

    std::string out;
    out += '{';
    AppendJsonField(out, "personality_version", request.personalityVersion, true);
    AppendJsonField(out, "career_version", request.careerVersion);
    out += ",\"candidates\":[";
    for (size_t index = 0; index < request.candidates.size(); ++index)
    {
        if (index)
            out += ',';
        PlayerbotCareerCandidateView const& candidate = request.candidates[index];
        out += '{';
        AppendJsonField(out, "token", candidate.token, true);
        AppendJsonField(out, "summary", candidate.summary);
        AppendJsonField(out, "maximum_spending_style", SpendingStyleName(candidate.maximumSpendingStyle));
        AppendJsonField(out, "market_eligible", static_cast<uint64>(candidate.marketEligible));
        AppendJsonField(out, "engagement", candidate.engagement);
        out += '}';
    }
    out += "]}";
    return out;
}

std::optional<ClaudeChat::CareerDecision> ClaudeChat::ParseCareerDecision(std::string const& content)
{
    std::optional<std::map<std::string, FlatJsonValue>> fields = FlatJsonParser(content).Parse();
    if (!fields || fields->size() != 2u)
        return std::nullopt;

    auto token = fields->find("candidate_token");
    auto style = fields->find("spending_style");
    if (token == fields->end() || style == fields->end() ||
        !token->second.isString || !style->second.isString ||
        token->second.text.find("career-") != 0u || token->second.text.size() > MAX_CAREER_TOKEN_BYTES)
        return std::nullopt;

    std::optional<PlayerbotRecipeSpendingStyle> parsedStyle = ParseSpendingStyle(style->second.text);
    if (!parsedStyle)
        return std::nullopt;

    return CareerDecision { token->second.text, *parsedStyle };
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

bool ClaudeChat::LegacyConversationalHookAllowed(bool socialGateEnabled)
{
    // One line, but named rather than inlined at four call sites: the rule is "the coordinator owns
    // responder selection while it is on", and a bare `!gate.enabled` at each hook does not say so.
    return !socialGateEnabled;
}

bool ClaudeChat::LegacyAmbientWorldAllowed(bool ambientConfigured, bool socialGateEnabled)
{
    return ambientConfigured && !socialGateEnabled;
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
        : config(std::move(bridgeConfig)), requests(config.queueCapacity), responses(config.queueCapacity),
          socialResponses(config.queueCapacity)
    {
    }

    /*
     * One queued request, in exactly one of the two shapes this bridge carries.
     *
     * A variant rather than a chat request with social fields hanging off it. The two share nothing
     * but a deadline, and a struct where half the fields are meaningless depending on a channel
     * enum is precisely the half described shape this protocol refuses everywhere else.
     */
    struct QueuedRequest
    {
        std::variant<ChatRequest, SocialRequest> payload;
        int64 expiresAtSteadyMs = 0;
    };

    BridgeConfig config;
    BoundedQueue<QueuedRequest> requests;
    BoundedQueue<ChatResponse> responses;
    BoundedQueue<SocialRawResponse> socialResponses;
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
            QueuedRequest request;
            if (!requests.WaitPop(request, std::chrono::milliseconds(200)))
                continue;

            if (SteadyNowMs() > request.expiresAtSteadyMs)
                continue;  // expired in queue: discard, fail closed

            SocialRequest const* const social = std::get_if<SocialRequest>(&request.payload);

            // A request that cannot be serialized within its budgets is discarded here rather than
            // sent for the far side to reject. Same fail closed posture as an expired one above.
            std::optional<std::string> const payload =
                social ? SerializeSocialRequest(*social, config.token)
                       : SerializeRequest(std::get<ChatRequest>(request.payload), config.token);
            if (!payload)
                continue;

            std::optional<std::vector<uint8>> const frame = EncodeFrame(*payload);
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

                std::optional<std::string> const answer = ReadFrame();
                if (!answer)
                {
                    // Timeout, closed connection, or oversized frame: the request is
                    // lost. Never fabricate a response.
                    DiscardSocket();
                    break;
                }

                if (social)
                {
                    // Handed back unparsed. Classifying it spends the regeneration budget, which
                    // lives with the transport on the world thread and not here.
                    socialResponses.TryPush(
                        SocialRawResponse{social->socialRequestToken, std::move(*answer), SteadyNowMs()});
                }
                else if (std::optional<ChatResponse> response = ParseResponsePayload(*answer, config.token))
                {
                    responses.TryPush(std::move(*response));
                }

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
    _impl->socialResponses.Stop();
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

    int64 const expiresAtSteadyMs = request.expiresAtSteadyMs;
    return _impl->requests.TryPush(Impl::QueuedRequest{std::move(request), expiresAtSteadyMs});
}

bool ClaudeChat::ClaudeBridge::TryEnqueueSocial(SocialRequest request, int64 expiresAtSteadyMs)
{
    if (_impl->stopped.load())
        return false;

    if (SteadyNowMs() > expiresAtSteadyMs)
        return false;

    return _impl->requests.TryPush(Impl::QueuedRequest{std::move(request), expiresAtSteadyMs});
}

std::vector<ClaudeChat::ChatResponse> ClaudeChat::ClaudeBridge::DrainResponses()
{
    std::vector<ChatResponse> drained;
    ChatResponse response;
    while (_impl->responses.TryPop(response))
        drained.push_back(std::move(response));

    return drained;
}

std::vector<ClaudeChat::SocialRawResponse> ClaudeChat::ClaudeBridge::DrainSocialResponses()
{
    std::vector<SocialRawResponse> drained;
    SocialRawResponse response;
    while (_impl->socialResponses.TryPop(response))
        drained.push_back(std::move(response));

    return drained;
}
