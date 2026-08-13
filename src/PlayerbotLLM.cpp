/*
 * This file is part of the mod-playerbots-llm module.
 */

#include "PlayerbotLLM.h"

#include "SharedDefines.h"
#include "utf8.h"

#include <boost/asio.hpp>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <map>
#include <thread>
#include <variant>

using boost::asio::ip::tcp;

// The wire's expansion ceiling is the game's, stated once in SharedDefines. Two spellings of "the
// last expansion" would drift exactly when a later core bump made the difference matter.
static_assert(PlayerbotLLM::MAX_SOCIAL_ACTIVE_EXPANSION == EXPANSION_WRATH_OF_THE_LICH_KING,
              "the wire's active_expansion ceiling must match the game's last expansion");

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

    std::string ChatChannelName(PlayerbotLLM::ChatChannel channel)
    {
        switch (channel)
        {
            case PlayerbotLLM::ChatChannel::Whisper:
                return "whisper";
            case PlayerbotLLM::ChatChannel::Party:
                return "party";
            case PlayerbotLLM::ChatChannel::World:
                return "world";
            case PlayerbotLLM::ChatChannel::Career:
                return "career";
            case PlayerbotLLM::ChatChannel::Social:
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

    bool FixedMicrodollarUsdIsValid(std::string const& value)
    {
        std::size_t const dot = value.find('.');
        if (dot == std::string::npos || dot == 0 || dot > 6 || value.size() != dot + 7)
            return false;

        if (dot > 1 && value.front() == '0')
            return false;

        for (std::size_t index = 0; index < value.size(); ++index)
        {
            if (index == dot)
                continue;
            if (value[index] < '0' || value[index] > '9')
                return false;
        }

        return true;
    }
}

std::string PlayerbotLLM::TruncateUtf8Bytes(std::string text, size_t maxBytes)
{
    if (text.size() > maxBytes)
        text.resize(maxBytes);

    // Cut at the first invalid position so a split multibyte sequence (or previously
    // invalid input) can never cross the wire.
    auto const invalid = utf8::find_invalid(text.begin(), text.end());
    text.resize(static_cast<size_t>(invalid - text.begin()));
    return text;
}

int64 PlayerbotLLM::SteadyNowMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

std::optional<std::string> PlayerbotLLM::BridgeTokenFromEnvironment()
{
    char const* raw = std::getenv("PLAYERBOTS_LLM_BRIDGE_TOKEN");
    if (!raw)
        return std::nullopt;

    std::string token(raw);

    // Both bounds at the one place the token enters the process, so nothing downstream has to be
    // the last thing standing between a misconfigured environment and every frame it would sign.
    if (!BridgeTokenIsUsable(token))
        return std::nullopt;

    return token;
}

std::optional<std::vector<uint8>> PlayerbotLLM::EncodeFrame(std::string const& payload)
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

std::optional<uint32> PlayerbotLLM::DecodeFrameLength(std::array<uint8, FRAME_HEADER_BYTES> const& header)
{
    uint32 const length = (static_cast<uint32>(header[0]) << 24) | (static_cast<uint32>(header[1]) << 16) |
                          (static_cast<uint32>(header[2]) << 8) | static_cast<uint32>(header[3]);
    if (length > MAX_FRAME_PAYLOAD_BYTES)
        return std::nullopt;

    return length;
}

std::optional<std::string> PlayerbotLLM::SerializeRequest(ChatRequest const& request, std::string const& token)
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

std::optional<PlayerbotLLM::ChatResponse> PlayerbotLLM::ParseResponsePayload(std::string const& payload,
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

bool PlayerbotLLM::ResponseKindIsValid(ResponseKind kind)
{
    switch (kind)
    {
        case ResponseKind::Chat:
        case ResponseKind::Career:
        case ResponseKind::Social:
        case ResponseKind::Biography:
        case ResponseKind::Memory:
        case ResponseKind::RoleplayAssessment:
            return true;
        default:
            break;
    }

    return false;
}

char const* PlayerbotLLM::ResponseKindName(ResponseKind kind)
{
    switch (kind)
    {
        case ResponseKind::Chat:
            return "chat";
        case ResponseKind::Career:
            return "career";
        case ResponseKind::Social:
            return "social";
        case ResponseKind::Biography:
            return "biography";
        case ResponseKind::Memory:
            return "memory";
        case ResponseKind::RoleplayAssessment:
            return "roleplay_assessment";
        default:
            break;
    }

    return "unknown";
}

std::optional<PlayerbotLLM::ResponseKind> PlayerbotLLM::ResponseKindFromName(std::string const& name)
{
    // Exact match only. A prefix or case insensitive match would let "social_draft" or "Career" be
    // accepted as something the sender did not say.
    if (name == "chat")
        return ResponseKind::Chat;
    if (name == "career")
        return ResponseKind::Career;
    if (name == "social")
        return ResponseKind::Social;
    if (name == "biography")
        return ResponseKind::Biography;
    if (name == "memory")
        return ResponseKind::Memory;
    if (name == "roleplay_assessment")
        return ResponseKind::RoleplayAssessment;

    return std::nullopt;
}

bool PlayerbotLLM::BridgeTokenIsUsable(std::string const& token)
{
    return token.size() >= MIN_BRIDGE_TOKEN_BYTES && token.size() <= MAX_BRIDGE_TOKEN_BYTES;
}

bool PlayerbotLLM::ActorIsAbsent(Actor const& actor)
{
    // Every field, not just the guid. A zero guid with a name attached is an orphan that still
    // travels and still describes somebody who is not there.
    return actor.guidCounter == 0 && actor.name.empty() && !actor.human;
}

bool PlayerbotLLM::ActorIsUsable(Actor const& actor)
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

bool PlayerbotLLM::SocialExchangeOutcomeIsValid(SocialExchangeOutcome outcome)
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

PlayerbotLLM::SocialExchangeOutcome PlayerbotLLM::SocialExchange::Classify(std::string const& payload,
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

bool PlayerbotLLM::SocialTransport::Submit(SocialRequest const& request)
{
    return SubmitAt(request, SteadyNowMs());
}

bool PlayerbotLLM::SocialTransport::SubmitAt(SocialRequest const& request, int64 nowMs)
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

std::vector<PlayerbotLLM::SocialTransport::Completed> PlayerbotLLM::SocialTransport::Drain()
{
    return Resolve(_bridge.DrainSocialResponses(), SteadyNowMs());
}

std::vector<PlayerbotLLM::SocialTransport::Completed> PlayerbotLLM::SocialTransport::Resolve(
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
         * A Message, an Emote, or deliberate Silence.
         *
         * The parser has already refused an answer carrying both and one carrying neither, so the
         * branch below is a read of which one arrived rather than a decision. Social schema 7 gives
         * deliberate silence the empty message and zero emote wire shape, which the structured
         * sidecar output can produce only through response_kind silence.
         */
        if (response.emoteId != 0)
        {
            result.kind = PlayerbotSocialOutputKind::Emote;
            result.emoteId = response.emoteId;
        }
        else if (!response.message.empty())
        {
            result.kind = PlayerbotSocialOutputKind::Message;
            result.text = response.message;
        }
        else
            result.kind = PlayerbotSocialOutputKind::Silence;
        result.channel = channel;
        result.callMetadata = response.callMetadata;
        result.contribution = response.contribution;
        result.claimSubject = response.claimSubject;
        result.citedEvidenceIds = response.citedEvidenceIds;

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

bool PlayerbotLLM::SocialEmoteIsSupported(uint32 emoteId)
{
    return std::find(SOCIAL_EMOTE_IDS.begin(), SOCIAL_EMOTE_IDS.end(), emoteId) != SOCIAL_EMOTE_IDS.end();
}

int64 PlayerbotLLM::SocialRequestDeadlineMs(int64 configuredDeadlineMs)
{
    return std::min<int64>(configuredDeadlineMs,
                           static_cast<int64>(PLAYERBOT_SOCIAL_PROVIDER_TIMEOUT_SECONDS) * 1000);
}

bool PlayerbotLLM::SocialAdmissionLaneIsValid(SocialAdmissionLane lane)
{
    return lane == SocialAdmissionLane::ImmediateHuman || lane == SocialAdmissionLane::Background;
}

char const* PlayerbotLLM::SocialAdmissionLaneName(SocialAdmissionLane lane)
{
    switch (lane)
    {
        case SocialAdmissionLane::ImmediateHuman:
            return "immediate_human";
        case SocialAdmissionLane::Background:
            return "background";
        case SocialAdmissionLane::Unknown:
            break;
    }

    return "unknown";
}

namespace
{
    char const* EvidenceSubjectName(PlayerbotSocialEvidenceSubjectRole subject)
    {
        switch (subject)
        {
            case PlayerbotSocialEvidenceSubjectRole::CandidateBot:
                return "candidate_bot";
            case PlayerbotSocialEvidenceSubjectRole::Participant:
                return "participant";
            case PlayerbotSocialEvidenceSubjectRole::Source:
                return "source";
        }
        return "unknown";
    }

    char const* EvidenceFactName(PlayerbotSocialEvidenceFactKind fact)
    {
        switch (fact)
        {
            case PlayerbotSocialEvidenceFactKind::Name: return "name";
            case PlayerbotSocialEvidenceFactKind::Race: return "race";
            case PlayerbotSocialEvidenceFactKind::CharacterClass: return "character_class";
            case PlayerbotSocialEvidenceFactKind::Level: return "level";
            case PlayerbotSocialEvidenceFactKind::Faction: return "faction";
            case PlayerbotSocialEvidenceFactKind::Zone: return "zone";
            case PlayerbotSocialEvidenceFactKind::Area: return "area";
            case PlayerbotSocialEvidenceFactKind::GroupRelation: return "group_relation";
            case PlayerbotSocialEvidenceFactKind::GuildRelation: return "guild_relation";
            case PlayerbotSocialEvidenceFactKind::CombatState: return "combat_state";
            case PlayerbotSocialEvidenceFactKind::Target: return "target";
            case PlayerbotSocialEvidenceFactKind::Visibility: return "visibility";
            case PlayerbotSocialEvidenceFactKind::Proximity: return "proximity";
            case PlayerbotSocialEvidenceFactKind::Progression: return "progression";
            case PlayerbotSocialEvidenceFactKind::Quest: return "quest";
            case PlayerbotSocialEvidenceFactKind::Item: return "item";
            case PlayerbotSocialEvidenceFactKind::Creature: return "creature";
            case PlayerbotSocialEvidenceFactKind::Objective: return "objective";
            case PlayerbotSocialEvidenceFactKind::Achievement: return "achievement";
        }
        return "unknown";
    }

    char const* EvidenceProvenanceName(PlayerbotSocialEvidenceProvenance provenance)
    {
        switch (provenance)
        {
            case PlayerbotSocialEvidenceProvenance::CurrentWorld:
                return "current_world";
            case PlayerbotSocialEvidenceProvenance::HumanObservation:
                return "human_observation";
            case PlayerbotSocialEvidenceProvenance::AuthoritativeSource:
                return "authoritative_source";
        }
        return "unknown";
    }

    char const* EvidenceScopeName(PlayerbotSocialPrivacyScope scope)
    {
        switch (scope)
        {
            case PlayerbotSocialPrivacyScope::Public: return "public";
            case PlayerbotSocialPrivacyScope::Party: return "party";
            case PlayerbotSocialPrivacyScope::Whisper: return "whisper";
        }
        return "unknown";
    }

    char const* MemoryInputStateName(PlayerbotSocialMemoryInputState state)
    {
        switch (state)
        {
            case PlayerbotSocialMemoryInputState::Pending: return "pending";
            case PlayerbotSocialMemoryInputState::Loaded: return "loaded";
            case PlayerbotSocialMemoryInputState::Absent: return "absent";
            case PlayerbotSocialMemoryInputState::Unavailable: return "unavailable";
        }
        return "unknown";
    }
}

bool PlayerbotLLM::SocialRequestIsUsable(SocialRequest const& request, std::string const& token)
{
    /*
     * Refused before anything is written. The sidecar enforces these too, but a bound checked only
     * on the far side means an oversize frame is built, sent, and rejected, and the caller learns
     * nothing about which request was at fault.
     */
    if (!BridgeTokenIsUsable(token))
        return false;

    if (request.socialRequestToken == 0 || !ActorIsUsable(request.bot) || request.bot.human || request.botLevel == 0 ||
        request.botLevel > MAX_SOCIAL_BOT_LEVEL || !SocialAdmissionLaneIsValid(request.admissionLane))
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

    if (!PlayerbotSocialChannelIsValid(static_cast<PlayerbotSocialChannel>(request.speakOnChannel)) ||
        !PlayerbotSocialGroundingEnvelopeIsValid(request.grounding) ||
        request.grounding.activeContentExpansion > MAX_SOCIAL_ACTIVE_EXPANSION)
        return false;

    PlayerbotSocialChannel const channel = static_cast<PlayerbotSocialChannel>(request.speakOnChannel);
    for (PlayerbotSocialEvidenceEntry const& evidence : request.grounding.entries)
    {
        if (!PlayerbotSocialMemoryIsRetrievableInChannel(evidence.scope, channel))
            return false;
        if (evidence.subjectRole == PlayerbotSocialEvidenceSubjectRole::CandidateBot &&
            evidence.subjectGuidCounter != request.bot.guidCounter)
            return false;
        if (evidence.subjectRole == PlayerbotSocialEvidenceSubjectRole::Participant &&
            (ActorIsAbsent(request.subject) || evidence.subjectGuidCounter != request.subject.guidCounter))
            return false;
    }

    return true;
}

namespace
{
    /*
     * The length of the longest prefix of `text` that fits in `limit` bytes and ends where a
     * character ends.
     *
     * Walks forward by whole sequences rather than cutting and backing off, which also makes a
     * sequence that is already malformed a stopping point: bytes past it cannot be decoded, and
     * carrying them would fail the same UTF-8 decode this exists to avoid.
     *
     * The core's utf8truncate does not fit here. It bounds a count of characters where this bounds
     * a count of bytes, which is what the frame is measured in, and it answers a string it cannot
     * decode by clearing the whole thing: a subject with one bad byte would become no subject at
     * all rather than the part that was fine.
     */
    size_t Utf8PrefixLength(std::string const& text, size_t limit)
    {
        size_t kept = 0;

        while (kept < text.size())
        {
            unsigned char const lead = static_cast<unsigned char>(text[kept]);

            size_t width = 0;
            if (lead < 0x80)
                width = 1;
            else if ((lead & 0xE0) == 0xC0)
                width = 2;
            else if ((lead & 0xF0) == 0xE0)
                width = 3;
            else if ((lead & 0xF8) == 0xF0)
                width = 4;
            else
                break;  // A continuation byte or an invalid lead: nothing from here on decodes.

            if (kept + width > text.size() || kept + width > limit)
                break;

            bool complete = true;
            for (size_t offset = 1; offset < width; ++offset)
                if ((static_cast<unsigned char>(text[kept + offset]) & 0xC0) != 0x80)
                    complete = false;

            if (!complete)
                break;

            kept += width;
        }

        return kept;
    }
}  // namespace

bool PlayerbotLLM::BiographyRequestIsUsable(BiographyRequest const& request, std::string const& token)
{
    if (!BridgeTokenIsUsable(token))
        return false;

    /*
     * A request with no token cannot be answered identifiably, so it is refused here rather than
     * sent and reconciled later. Definition of Done 2 rests on the echo, and a zero token would
     * make every completion for this bot indistinguishable from every other.
     */
    if (request.biographyRequestToken == 0)
        return false;

    if (request.botGuidCounter == 0 || request.botLevel == 0 || request.botLevel > MAX_SOCIAL_BOT_LEVEL ||
        request.activeContentExpansion > MAX_SOCIAL_ACTIVE_EXPANSION)
        return false;

    // The name reaches the TRUSTED half of the prompt, because the bot has to be told who it is,
    // so its bound is a security property rather than a formatting preference.
    if (request.characterName.empty() || request.characterName.size() > MAX_ACTOR_NAME_BYTES)
        return false;

    return true;
}

std::optional<std::string> PlayerbotLLM::SerializeBiographyRequest(BiographyRequest const& request,
                                                                 std::string const& token)
{
    if (!BiographyRequestIsUsable(request, token))
        return std::nullopt;

    std::string out;
    out.reserve(256);
    out += '{';
    AppendJsonField(out, "schema_version", SCHEMA_VERSION, true);
    AppendJsonField(out, "token", token);
    AppendJsonField(out, "kind", std::string(ResponseKindName(ResponseKind::Biography)));
    AppendJsonField(out, "biography_request_token", request.biographyRequestToken);
    AppendJsonField(out, "bot_guid", request.botGuidCounter);
    AppendJsonField(out, "character_name", request.characterName);
    AppendJsonField(out, "race_id", static_cast<uint64>(request.raceId));
    AppendJsonField(out, "class_id", static_cast<uint64>(request.classId));
    AppendJsonField(out, "gender_id", static_cast<uint64>(request.genderId));
    AppendJsonField(out, "bot_level", static_cast<uint64>(request.botLevel));
    AppendJsonField(out, "active_expansion", static_cast<uint64>(request.activeContentExpansion));
    out += '}';
    return out;
}

namespace
{
    char const* PrivacyScopeName(PlayerbotSocialPrivacyScope scope)
    {
        switch (scope)
        {
            case PlayerbotSocialPrivacyScope::Party:
                return "party";
            case PlayerbotSocialPrivacyScope::Whisper:
                return "whisper";
            case PlayerbotSocialPrivacyScope::Public:
                return "public";
            default:
                break;
        }

        /*
         * A value from outside the enum is narrowed to the most private name rather than the most
         * public one. The far side filters again by scope, and a corrupt value read as "public"
         * would be a fact repeated in a zone on the strength of a byte nobody wrote.
         */
        return "whisper";
    }

    /*
     * A bounded string list, or nothing when it is empty.
     *
     * Bounded in COUNT here as well as per entry, and not only because the producer already is: the
     * far side refuses a list longer than its declared maximum and drops the whole context with it,
     * so this is the last place a producer that forgot its own bound can be caught before the wire.
     */
    void AppendJsonStringList(std::string& out, char const* key, std::vector<std::string> const& values)
    {
        if (values.empty())
            return;

        out += ",\"";
        out += key;
        out += "\":[";
        bool first = true;
        std::size_t written = 0;
        for (std::string const& value : values)
        {
            if (written >= PlayerbotLLM::MAX_SOCIAL_CONTEXT_ENTRIES)
                break;

            if (!first)
                out += ',';
            AppendEscapedJsonString(
                out, value.substr(0, Utf8PrefixLength(value, PlayerbotLLM::MAX_SOCIAL_CONTEXT_ENTRY_BYTES)));
            first = false;
            ++written;
        }
        out += ']';
    }

    std::optional<std::string> EncodeFictionalIdentity(
        PlayerbotFictionalIdentityPromptContext const& identity)
    {
        bool const hasAge = identity.age.has_value();
        bool const hasCountry = identity.homeCountry.has_value();
        std::string requestName;

        switch (identity.request)
        {
            case PlayerbotFictionalIdentityRequest::None:
                return hasAge || hasCountry ? std::nullopt : std::optional<std::string>(std::string());
            case PlayerbotFictionalIdentityRequest::Age:
                if (hasCountry)
                    return std::nullopt;
                requestName = "age";
                break;
            case PlayerbotFictionalIdentityRequest::HomeCountry:
                if (hasAge)
                    return std::nullopt;
                requestName = "home_country";
                break;
            case PlayerbotFictionalIdentityRequest::AgeAndHomeCountry:
                requestName = "age_and_home_country";
                break;
            default:
                return std::nullopt;
        }

        if (hasAge && (*identity.age < PLAYERBOT_FICTIONAL_IDENTITY_MIN_AGE ||
                       *identity.age > PLAYERBOT_FICTIONAL_IDENTITY_MAX_AGE))
            return std::nullopt;

        if (hasCountry && !PlayerbotFictionalIdentity::IsApprovedCountry(*identity.homeCountry))
            return std::nullopt;

        std::string encoded;
        AppendJsonField(encoded, "fictional_identity_request", requestName, true);
        if (hasAge)
            AppendJsonField(encoded, "fictional_age", static_cast<uint64>(*identity.age));
        if (hasCountry)
            AppendJsonField(encoded, "fictional_home_country", *identity.homeCountry);
        return encoded;
    }
}

std::optional<std::string> PlayerbotLLM::EncodeSocialContext(PlayerbotSocialRequestContext const& context)
{
    /*
     * The authority gate comes before any assembly. A mode or expansion outside the wire's
     * vocabulary refuses the whole context rather than travelling as a spelling the sidecar would
     * treat as malformed, and refusing here is what keeps "reject an invalid value" and "omit an
     * invalid value" from ever being the same behaviour.
     */
    if (!PlayerbotRoleplayPromptModeIsValid(context.promptMode) ||
        context.activeContentExpansion > MAX_SOCIAL_ACTIVE_EXPANSION)
        return std::nullopt;

    /*
     * Assembled most specific last, so that dropping from the end to fit the total bound sheds the
     * least useful block first. The persona is written first for the same reason: whatever else has
     * to go, a bot still sounds like itself.
     */
    std::string out;
    out.reserve(512);
    out += '{';

    bool first = true;
    auto appendEntry = [&](char const* key, std::string const& value)
    {
        if (value.empty())
            return;

        AppendJsonField(out, key,
                        value.substr(0, Utf8PrefixLength(value, MAX_SOCIAL_CONTEXT_ENTRY_BYTES)), first);
        first = false;
    };

    appendEntry("persona", context.persona);

    std::optional<std::string> const identity = EncodeFictionalIdentity(context.fictionalIdentity);
    if (identity && !identity->empty())
    {
        if (!first)
            out += ',';
        out += *identity;
        first = false;
    }

    appendEntry("relationship", context.relationship);
    appendEntry("starter", context.starter);

    /*
     * The lists come after the scalars, and the leading comma inside the helper is why: by this
     * point at least one scalar has almost always been written, and a context of lists alone is
     * repaired below rather than complicating every call.
     */
    std::string lists;
    AppendJsonStringList(lists, "nearby", context.nearby);
    AppendJsonStringList(lists, "thread", context.thread);

    if (!context.memories.empty())
    {
        lists += ",\"memories\":[";
        bool firstMemory = true;
        std::size_t writtenMemories = 0;
        for (PlayerbotSocialContextMemory const& memory : context.memories)
        {
            // Bounded here too, for the reason the string lists are: the far side drops the whole
            // context over a list one entry too long, and this is the last place to catch it.
            if (memory.text.empty() || writtenMemories >= MAX_SOCIAL_CONTEXT_ENTRIES)
                continue;

            if (!firstMemory)
                lists += ',';

            lists += "{\"text\":";
            AppendEscapedJsonString(
                lists, memory.text.substr(0, Utf8PrefixLength(memory.text, MAX_SOCIAL_CONTEXT_ENTRY_BYTES)));
            lists += ",\"scope\":\"";
            lists += PrivacyScopeName(memory.scope);
            lists += "\"}";
            firstMemory = false;
            ++writtenMemories;
        }
        lists += ']';

        // Every entry was empty, so the key would have been an empty array the far side has no use
        // for. Rewinding is cheaper than deciding in advance whether any survived the bound.
        if (firstMemory)
            lists.resize(lists.size() - std::string(",\"memories\":[]").size());
    }

    if (!lists.empty())
    {
        // A context of lists alone starts with the helper's separator, which would be a leading
        // comma after the brace.
        out += first ? lists.substr(1) : lists;
        first = false;
    }

    if (first)
        return std::string();

    /*
     * Authority last, after the check above: an entirely empty assembly still encodes to the empty
     * string, because "nothing was assembled" already has that spelling and the far side answers an
     * absent context in ordinary voice, which is the fail-closed floor this feature stands on.
     * Written by re-encoding on every trim below, so no amount of shedding removes it.
     */
    AppendJsonField(out, "prompt_mode", std::string(PlayerbotRoleplayPromptModeName(context.promptMode)));
    AppendJsonField(out, "active_expansion", static_cast<uint64>(context.activeContentExpansion));

    out += '}';

    /*
     * The last resort, and it is a real one: twelve bounded memories plus a bounded persona and
     * relationship exceed the total. Blocks are shed from the end, which is the order they were
     * written in, so the persona is the last thing to go and in practice never does.
     */
    if (out.size() > MAX_SOCIAL_CONTEXT_BYTES && !context.memories.empty())
    {
        PlayerbotSocialRequestContext shorter = context;
        shorter.memories.pop_back();
        return EncodeSocialContext(shorter);
    }

    if (out.size() > MAX_SOCIAL_CONTEXT_BYTES)
    {
        PlayerbotSocialRequestContext shorter = context;
        if (!shorter.thread.empty())
            shorter.thread.pop_back();
        else if (!shorter.nearby.empty())
            shorter.nearby.pop_back();
        else if (!shorter.starter.empty())
            shorter.starter.clear();
        else if (!shorter.relationship.empty())
            shorter.relationship.clear();
        else
            return std::string();

        return EncodeSocialContext(shorter);
    }

    return out;
}

std::string PlayerbotLLM::EncodeStarterContext(std::string const& subject)
{
    if (subject.empty())
        return std::string();

    /*
     * A starter is bot-initiated ordinary chat, so its authority is the ordinary floor by
     * definition: bot-initiated roleplay does not exist, and the expansion is unread under the
     * ordinary mode. The fields still have to be present, because the sidecar requires them on
     * every structured context and would otherwise drop the starter on the only surface it uses.
     */
    std::string out;
    out += "{\"starter\":";
    AppendEscapedJsonString(out, subject.substr(0, Utf8PrefixLength(subject, MAX_SOCIAL_CONTEXT_ENTRY_BYTES)));
    out += ",\"prompt_mode\":\"ordinary\",\"active_expansion\":0}";
    return out;
}

std::optional<std::string> PlayerbotLLM::SerializeSocialRequest(SocialRequest const& request,
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
    AppendJsonField(out, "schema_version", SOCIAL_SCHEMA_VERSION, true);
    AppendJsonField(out, "token", token);
    AppendJsonField(out, "kind", std::string(ResponseKindName(ResponseKind::Social)));
    AppendJsonField(out, "social_request_token", request.socialRequestToken);
    AppendJsonField(out, "bot_guid", request.bot.guidCounter);
    AppendJsonField(out, "bot_name", request.bot.name);
    AppendJsonField(out, "bot_human", request.bot.human ? uint64{1} : uint64{0});
    AppendJsonField(out, "bot_level", static_cast<uint64>(request.botLevel));
    AppendJsonField(out, "subject_guid", request.subject.guidCounter);
    AppendJsonField(out, "subject_name", request.subject.name);
    AppendJsonField(out, "subject_human", request.subject.human ? uint64{1} : uint64{0});
    AppendJsonField(out, "admission_lane", std::string(SocialAdmissionLaneName(request.admissionLane)));
    AppendJsonField(out, "speak_on_channel", static_cast<uint64>(request.speakOnChannel));
    AppendJsonField(out, "thread_id", request.threadPublicId);
    AppendJsonField(out, "context", request.context);
    out += ",\"evidence\":[";
    bool firstEvidence = true;
    for (PlayerbotSocialEvidenceEntry const& evidence : request.grounding.entries)
    {
        if (!firstEvidence)
            out += ',';
        out += '{';
        AppendJsonField(out, "id", evidence.id, true);
        AppendJsonField(out, "subject", std::string(EvidenceSubjectName(evidence.subjectRole)));
        AppendJsonField(out, "fact", std::string(EvidenceFactName(evidence.factKind)));
        AppendJsonField(out, "value", evidence.value);
        AppendJsonField(out, "provenance", std::string(EvidenceProvenanceName(evidence.provenance)));
        AppendJsonField(out, "scope", std::string(EvidenceScopeName(evidence.scope)));
        AppendJsonField(out, "observed_at", evidence.atUnixSeconds);
        out += '}';
        firstEvidence = false;
    }
    out += ']';

    out += ",\"transcript_event_ids\":[";
    bool firstEvent = true;
    for (std::string const& eventPublicId : request.grounding.transcriptEventPublicIds)
    {
        if (!firstEvent)
            out += ',';
        AppendEscapedJsonString(out, eventPublicId);
        firstEvent = false;
    }
    out += ']';
    AppendJsonField(out, "profile_load_state",
                    std::string(PlayerbotSocialProfileLoadStateName(request.grounding.profileLoadState)));
    AppendJsonField(out, "memory_input_state",
                    std::string(MemoryInputStateName(request.grounding.memoryInputState)));
    AppendJsonField(out, "active_content_expansion",
                    static_cast<uint64>(request.grounding.activeContentExpansion));
    out += '}';
    return out;
}

std::optional<PlayerbotLLM::BiographyResponse> PlayerbotLLM::ParseBiographyResponsePayload(
    std::string const& payload, std::string const& expectedToken, uint64 expectedRequestToken,
    uint64 expectedBotGuidCounter)
{
    std::optional<std::map<std::string, FlatJsonValue>> fields = FlatJsonParser(payload).Parse();
    if (!fields)
        return std::nullopt;

    /*
     * Five protocol keys plus the eight generated ones. The parser already refuses duplicates, and
     * an exact count refuses unknown keys, so nothing rides along unnoticed. That is what makes an
     * instruction field impossible rather than merely ignored.
     */
    if (fields->size() != 5 + BIOGRAPHY_FIELD_NAMES.size())
        return std::nullopt;

    auto const schemaIt = fields->find("schema_version");
    auto const tokenIt = fields->find("token");
    auto const kindIt = fields->find("kind");
    auto const requestIt = fields->find("biography_request_token");
    auto const botIt = fields->find("bot_guid");

    if (schemaIt == fields->end() || tokenIt == fields->end() || kindIt == fields->end() ||
        requestIt == fields->end() || botIt == fields->end())
        return std::nullopt;

    if (schemaIt->second.isString || schemaIt->second.number != SCHEMA_VERSION)
        return std::nullopt;

    if (!BridgeTokenIsUsable(expectedToken))
        return std::nullopt;

    if (!tokenIt->second.isString || !BridgeTokenIsUsable(tokenIt->second.text) ||
        !ConstantTimeEquals(tokenIt->second.text, expectedToken))
        return std::nullopt;

    if (!kindIt->second.isString)
        return std::nullopt;

    std::optional<ResponseKind> const kind = ResponseKindFromName(kindIt->second.text);
    if (!kind || *kind != ResponseKind::Biography)
        return std::nullopt;

    if (requestIt->second.isString || requestIt->second.number != expectedRequestToken)
        return std::nullopt;

    if (botIt->second.isString || botIt->second.number != expectedBotGuidCounter)
        return std::nullopt;

    BiographyResponse response;
    response.biographyRequestToken = requestIt->second.number;
    response.botGuidCounter = botIt->second.number;
    response.fields.reserve(BIOGRAPHY_FIELD_NAMES.size());

    // Walked in the contract's order rather than the map's, so the result is ordered by the
    // agreement between the two sides instead of by however the keys happened to sort.
    for (char const* name : BIOGRAPHY_FIELD_NAMES)
    {
        auto const field = fields->find(name);
        if (field == fields->end() || !field->second.isString)
            return std::nullopt;

        /*
         * Refused whole rather than per field. A biography with one bad field is not a biography
         * with seven good ones: the assembler on the other side would reject it anyway, and
         * dropping the field here would hand it a hole to fill with a default.
         */
        if (field->second.text.empty() || field->second.text.size() > MAX_BIOGRAPHY_FIELD_BYTES ||
            !IsSingleCleanLine(field->second.text))
            return std::nullopt;

        response.fields.push_back(BiographyResponseField{name, field->second.text});
    }

    return response;
}

std::optional<PlayerbotLLM::SocialResponse> PlayerbotLLM::ParseSocialResponsePayload(std::string const& payload,
                                                                                 std::string const& expectedToken,
                                                                                 uint64 expectedRequestToken,
                                                                                 uint64 expectedBotGuidCounter)
{
    std::optional<std::map<std::string, FlatJsonValue>> fields = FlatJsonParser(payload).Parse();
    if (!fields)
        return std::nullopt;

    // A regeneration has the nine control fields. A deliverable answer adds the complete seven
    // field call document, three proposal fields, and one field per cited request-local evidence id.
    if (fields->size() != 9 &&
        (fields->size() < 19 || fields->size() > 19 + MAX_SOCIAL_EVIDENCE_ENTRIES))
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

    if (schemaIt->second.isString || schemaIt->second.number != SOCIAL_SCHEMA_VERSION)
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
    {
        if (fields->size() != 9)
            return std::nullopt;
        return response;
    }

    auto const modelIt = fields->find("model");
    auto const latencyIt = fields->find("provider_latency_ms");
    auto const inputIt = fields->find("input_tokens");
    auto const outputIt = fields->find("output_tokens");
    auto const cacheCreationIt = fields->find("cache_creation_input_tokens");
    auto const cacheReadIt = fields->find("cache_read_input_tokens");
    auto const costIt = fields->find("cost_usd");
    auto const contributionIt = fields->find("contribution");
    auto const claimSubjectIt = fields->find("claim_subject");
    auto const citationCountIt = fields->find("citation_count");
    if (modelIt == fields->end() || latencyIt == fields->end() || inputIt == fields->end() ||
        outputIt == fields->end() || cacheCreationIt == fields->end() || cacheReadIt == fields->end() ||
        costIt == fields->end() || contributionIt == fields->end() || claimSubjectIt == fields->end() ||
        citationCountIt == fields->end())
        return std::nullopt;

    if (!modelIt->second.isString || modelIt->second.text.empty() ||
        modelIt->second.text.size() > PLAYERBOT_SOCIAL_MODEL_BYTES || !IsSingleCleanLine(modelIt->second.text))
        return std::nullopt;

    if (latencyIt->second.isString || inputIt->second.isString || outputIt->second.isString ||
        cacheCreationIt->second.isString || cacheReadIt->second.isString)
        return std::nullopt;

    if (!costIt->second.isString || !FixedMicrodollarUsdIsValid(costIt->second.text))
        return std::nullopt;

    if (!contributionIt->second.isString || !claimSubjectIt->second.isString || citationCountIt->second.isString ||
        citationCountIt->second.number > MAX_SOCIAL_EVIDENCE_ENTRIES ||
        fields->size() != 19 + citationCountIt->second.number)
        return std::nullopt;

    std::optional<PlayerbotSocialContributionFunction> const contribution =
        PlayerbotSocialContributionFunctionFromName(contributionIt->second.text);
    std::optional<PlayerbotSocialClaimSubject> const claimSubject =
        PlayerbotSocialClaimSubjectFromName(claimSubjectIt->second.text);
    if (!contribution || !claimSubject)
        return std::nullopt;

    response.contribution = *contribution;
    response.claimSubject = *claimSubject;
    for (uint64 index = 0; index < citationCountIt->second.number; ++index)
    {
        auto const citation = fields->find("citation_" + std::to_string(index));
        if (citation == fields->end() || !citation->second.isString || citation->second.text.size() < 2 ||
            citation->second.text.front() != 'g' ||
            !std::all_of(citation->second.text.begin() + 1, citation->second.text.end(),
                         [](char character) { return character >= '0' && character <= '9'; }) ||
            std::find(response.citedEvidenceIds.begin(), response.citedEvidenceIds.end(), citation->second.text) !=
                response.citedEvidenceIds.end())
            return std::nullopt;

        response.citedEvidenceIds.push_back(citation->second.text);
    }

    response.callMetadata = PlayerbotSocialCallMetadata{
        modelIt->second.text,
        latencyIt->second.number,
        inputIt->second.number,
        outputIt->second.number,
        cacheCreationIt->second.number,
        cacheReadIt->second.number,
        costIt->second.text,
    };

    /*
     * Exactly one answer. Both is the sidecar hedging, and choosing one here would be inventing an
     * intention it did not express; the coordinator would drop the text anyway, so a frame carrying
     * both is refused where the caller can still be told which request was at fault.
     *
     * Neither is deliberate silence in Social schema 7. The sidecar's strict structured output is
     * what distinguishes it from an accidentally omitted model field before this flat response is
     * encoded.
     */
    if (response.emoteId != 0)
    {
        if (!response.message.empty())
            return std::nullopt;

        // The allowlist, enforced where the value is read rather than only where it was chosen.
        if (!SocialEmoteIsSupported(response.emoteId))
            return std::nullopt;

        if (response.contribution != PlayerbotSocialContributionFunction::Gesture ||
            response.claimSubject != PlayerbotSocialClaimSubject::None || !response.citedEvidenceIds.empty())
            return std::nullopt;

        return response;
    }

    if (response.message.size() > MAX_RESPONSE_MESSAGE_BYTES ||
        !IsSingleCleanLine(response.message))
        return std::nullopt;

    if (response.message.empty())
    {
        if (response.contribution != PlayerbotSocialContributionFunction::None ||
            response.claimSubject != PlayerbotSocialClaimSubject::None || !response.citedEvidenceIds.empty())
            return std::nullopt;
        return response;
    }

    if (response.contribution == PlayerbotSocialContributionFunction::Gesture ||
        response.contribution == PlayerbotSocialContributionFunction::None ||
        (response.claimSubject == PlayerbotSocialClaimSubject::None && !response.citedEvidenceIds.empty()) ||
        (response.claimSubject != PlayerbotSocialClaimSubject::None && response.citedEvidenceIds.empty()))
        return std::nullopt;

    return response;
}

std::optional<std::string> PlayerbotLLM::SerializeCareerRequestContent(PlayerbotCareerPlanRequest const& request)
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

std::optional<PlayerbotLLM::CareerDecision> PlayerbotLLM::ParseCareerDecision(std::string const& content)
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

std::optional<uint64> PlayerbotLLM::SelectMilestoneSpeaker(MilestoneEventId const& eventId,
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

std::optional<uint64> PlayerbotLLM::SelectAmbientSpeaker(uint64 occurrence,
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

PlayerbotLLM::AmbientCadence::AmbientCadence(uint32 messagesPerHour, int64 startMs)
{
    if (!messagesPerHour || messagesPerHour > MAX_AMBIENT_MESSAGES_PER_HOUR)
        return;

    _intervalMs = 60 * 60 * 1000 / messagesPerHour;
    _nextDueMs = startMs + _intervalMs;
}

bool PlayerbotLLM::AmbientCadence::IsValid() const
{
    return _intervalMs > 0;
}

bool PlayerbotLLM::AmbientCadence::TryConsumeDueSlot(int64 nowMs)
{
    if (!IsValid() || nowMs < _nextDueMs)
        return false;

    _nextDueMs = nowMs + _intervalMs;
    return true;
}

bool PlayerbotLLM::ShouldEnqueueAmbient(bool humanOnline,
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

bool PlayerbotLLM::LegacyConversationalHookAllowed(bool socialGateEnabled)
{
    // One line, but named rather than inlined at four call sites: the rule is "the coordinator owns
    // responder selection while it is on", and a bare `!gate.enabled` at each hook does not say so.
    return !socialGateEnabled;
}

bool PlayerbotLLM::LegacyAmbientWorldAllowed(bool ambientConfigured, bool socialGateEnabled)
{
    return ambientConfigured && !socialGateEnabled;
}

bool PlayerbotLLM::RecentEventIdSet::Insert(MilestoneEventId const& eventId)
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

std::optional<std::string> PlayerbotLLM::WhisperLLMText(std::string const& message,
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

std::optional<std::string> PlayerbotLLM::ParseLlmWhisper(std::string const& message)
{
    static constexpr char PREFIX[] = "llm ";
    if (message.rfind(PREFIX, 0) != 0)
        return std::nullopt;

    std::string text = message.substr(sizeof(PREFIX) - 1);
    if (text.empty() || text.find_first_not_of(' ') == std::string::npos)
        return std::nullopt;

    return text;
}

std::optional<std::pair<std::string, std::string>> PlayerbotLLM::ParseLlmParty(std::string const& message)
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

bool PlayerbotLLM::ShouldDeliver(ChatChannel channel, DeliverySnapshot const& snapshot)
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

bool PlayerbotLLM::GroupCooldownTracker::TryBegin(uint64 groupId, int64 nowMs, int64 cooldownMs)
{
    auto it = _lastBeginMs.find(groupId);
    if (it != _lastBeginMs.end() && nowMs - it->second < cooldownMs)
        return false;

    _lastBeginMs[groupId] = nowMs;
    return true;
}

// --- Bridge worker ---

struct PlayerbotLLM::Bridge::Impl
{
    explicit Impl(BridgeConfig bridgeConfig)
        : config(std::move(bridgeConfig)), requests(config.queueCapacity), responses(config.queueCapacity),
          socialResponses(config.queueCapacity), biographyResponses(config.queueCapacity),
          memoryResponses(config.queueCapacity), assessmentResponses(config.queueCapacity)
    {
    }

    /*
     * One queued request, in exactly one of the three shapes this bridge carries.
     *
     * A variant rather than a chat request with social and biography fields hanging off it. The
     * three share nothing but a deadline, and a struct where most of the fields are meaningless
     * depending on a channel enum is precisely the half described shape this protocol refuses
     * everywhere else.
     */
    struct QueuedRequest
    {
        std::variant<ChatRequest, SocialRequest, BiographyRequest, MemoryRequest, RoleplayAssessmentRequest>
            payload;
        int64 expiresAtSteadyMs = 0;
    };

    BridgeConfig config;
    BoundedQueue<QueuedRequest> requests;
    BoundedQueue<ChatResponse> responses;
    BoundedQueue<SocialRawResponse> socialResponses;

    /*
     * Parsed in the worker rather than handed back raw, unlike the social lane.
     *
     * The social lane defers parsing because classifying a payload spends the request's
     * regeneration budget, which lives on the world thread. A biography has no regeneration budget
     * and no conversation to fall out of, and the worker already holds the token and bot this
     * request named, so it can do the identity check itself and hand back only answers that
     * actually belong to the request it sent.
     */
    BoundedQueue<BiographyResponse> biographyResponses;
    BoundedQueue<MemoryResponse> memoryResponses;

    // Parsed in the worker for the reason a biography is: the assessment token of the very request
    // that was sent is in scope, so an answer to anything else never reaches the world thread.
    BoundedQueue<RoleplayAssessmentResponse> assessmentResponses;
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
            BiographyRequest const* const biography = std::get_if<BiographyRequest>(&request.payload);
            MemoryRequest const* const memory = std::get_if<MemoryRequest>(&request.payload);
            RoleplayAssessmentRequest const* const assessment =
                std::get_if<RoleplayAssessmentRequest>(&request.payload);

            // A request that cannot be serialized within its budgets is discarded here rather than
            // sent for the far side to reject. Same fail closed posture as an expired one above.
            std::optional<std::string> payload;
            if (social)
                payload = SerializeSocialRequest(*social, config.token);
            else if (biography)
                payload = SerializeBiographyRequest(*biography, config.token);
            else if (memory)
                payload = SerializeMemoryRequest(*memory, config.token);
            else if (assessment)
                payload = SerializeRoleplayAssessmentRequest(*assessment, config.token);
            else
                payload = SerializeRequest(std::get<ChatRequest>(request.payload), config.token);

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
                else if (biography)
                {
                    /*
                     * Parsed here, because everything needed to judge it is here: the token and the
                     * bot this very request named. An answer for a different request or a different
                     * bot is dropped rather than queued, so the world thread never sees one.
                     *
                     * There is no lateness check. A biography is generated once and kept, so a slow
                     * answer is still the right answer; the coordinator's own token fence is what
                     * refuses one that a fresh request has already superseded.
                     */
                    if (std::optional<BiographyResponse> parsed = ParseBiographyResponsePayload(
                            *answer, config.token, biography->biographyRequestToken,
                            biography->botGuidCounter))
                        biographyResponses.TryPush(std::move(*parsed));
                }
                else if (memory)
                {
                    /*
                     * Parsed here for the reason a biography is: the token and the bot this very
                     * request named are both in scope, so an answer to a different request never
                     * reaches the world thread at all.
                     *
                     * No lateness check either, and here that matters more than it does for a
                     * backstory: the conversation this describes is already over and its buffer is
                     * already cleared, so a slow answer is still the only answer. The coordinator's
                     * own token fence is what refuses one whose thread has since been pruned.
                     */
                    if (std::optional<MemoryResponse> parsed = ParseMemoryResponsePayload(
                            *answer, config.token, memory->memoryRequestToken, memory->botGuidCounter))
                        memoryResponses.TryPush(std::move(*parsed));
                }
                else if (assessment)
                {
                    /*
                     * Parsed here for the reason a biography is. There is no lateness judgement
                     * either: the coordinator's own staleness fences decide whether the assessed
                     * line is still worth acting on.
                     */
                    if (std::optional<RoleplayAssessmentResponse> parsed = ParseRoleplayAssessmentResponsePayload(
                            *answer, config.token, assessment->assessmentToken))
                        assessmentResponses.TryPush(std::move(*parsed));
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

PlayerbotLLM::Bridge::Bridge(BridgeConfig config) : _impl(std::make_unique<Impl>(std::move(config)))
{
}

PlayerbotLLM::Bridge::~Bridge()
{
    Stop();
}

void PlayerbotLLM::Bridge::Start()
{
    if (_impl->started.exchange(true))
        return;

    _impl->worker = std::jthread([impl = _impl.get()](std::stop_token stopToken) { impl->Run(stopToken); });
}

void PlayerbotLLM::Bridge::Stop()
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

bool PlayerbotLLM::Bridge::TryEnqueue(ChatRequest request)
{
    if (_impl->stopped.load())
        return false;

    if (SteadyNowMs() > request.expiresAtSteadyMs)
        return false;

    int64 const expiresAtSteadyMs = request.expiresAtSteadyMs;
    return _impl->requests.TryPush(Impl::QueuedRequest{std::move(request), expiresAtSteadyMs});
}

bool PlayerbotLLM::Bridge::TryEnqueueSocial(SocialRequest request, int64 expiresAtSteadyMs)
{
    if (_impl->stopped.load())
        return false;

    if (SteadyNowMs() > expiresAtSteadyMs)
        return false;

    return _impl->requests.TryPush(Impl::QueuedRequest{std::move(request), expiresAtSteadyMs});
}

std::vector<PlayerbotLLM::ChatResponse> PlayerbotLLM::Bridge::DrainResponses()
{
    std::vector<ChatResponse> drained;
    ChatResponse response;
    while (_impl->responses.TryPop(response))
        drained.push_back(std::move(response));

    return drained;
}

bool PlayerbotLLM::Bridge::TryEnqueueBiography(BiographyRequest request, int64 expiresAtSteadyMs)
{
    if (_impl->stopped.load())
        return false;

    if (SteadyNowMs() > expiresAtSteadyMs)
        return false;

    return _impl->requests.TryPush(Impl::QueuedRequest{std::move(request), expiresAtSteadyMs});
}

std::vector<PlayerbotLLM::BiographyResponse> PlayerbotLLM::Bridge::DrainBiographyResponses()
{
    std::vector<BiographyResponse> drained;
    BiographyResponse response;
    while (_impl->biographyResponses.TryPop(response))
        drained.push_back(std::move(response));

    return drained;
}

bool PlayerbotLLM::Bridge::TryEnqueueMemory(MemoryRequest request, int64 expiresAtSteadyMs)
{
    if (_impl->stopped.load())
        return false;

    if (SteadyNowMs() > expiresAtSteadyMs)
        return false;

    return _impl->requests.TryPush(Impl::QueuedRequest{std::move(request), expiresAtSteadyMs});
}

std::vector<PlayerbotLLM::MemoryResponse> PlayerbotLLM::Bridge::DrainMemoryResponses()
{
    std::vector<MemoryResponse> drained;
    MemoryResponse response;
    while (_impl->memoryResponses.TryPop(response))
        drained.push_back(std::move(response));

    return drained;
}

std::vector<PlayerbotLLM::SocialRawResponse> PlayerbotLLM::Bridge::DrainSocialResponses()
{
    std::vector<SocialRawResponse> drained;
    SocialRawResponse response;
    while (_impl->socialResponses.TryPop(response))
        drained.push_back(std::move(response));

    return drained;
}

bool PlayerbotLLM::MemoryRequestIsUsable(MemoryRequest const& request, std::string const& token)
{
    if (!BridgeTokenIsUsable(token))
        return false;

    // Without a token the reply cannot be matched to the request that asked for it, and every
    // completion for this bot becomes indistinguishable from every other.
    if (request.memoryRequestToken == 0 || request.botGuidCounter == 0)
        return false;

    if (request.botName.empty() || request.botName.size() > MAX_ACTOR_NAME_BYTES)
        return false;

    if (request.threadPublicId.empty() || request.threadPublicId.size() > MAX_THREAD_ID_BYTES)
        return false;

    /*
     * Whisper is refused here as well as by the buffer that will not hold the text and the sidecar
     * schema that will not accept the request. Three independent refusals, because sending a
     * private message to a provider cannot be undone once it has happened.
     */
    if (request.scope != PlayerbotSocialPrivacyScope::Public && request.scope != PlayerbotSocialPrivacyScope::Party)
        return false;

    if (request.subjects.empty() || request.subjects.size() > MAX_MEMORY_SUBJECTS)
        return false;

    for (MemorySubject const& subject : request.subjects)
    {
        // The name reaches the TRUSTED half of the prompt so a memory can be attributed, which
        // makes its bound a security property rather than a formatting preference.
        if (subject.guidCounter == 0 || subject.name.empty() || subject.name.size() > MAX_ACTOR_NAME_BYTES)
            return false;
    }

    if (request.thread.empty() || request.thread.size() > MAX_MEMORY_THREAD_LINES)
        return false;

    for (std::size_t index = 0; index < request.thread.size(); ++index)
    {
        MemoryLine const& line = request.thread[index];
        if (line.speakerGuidCounter == 0 || line.speakerName.empty() ||
            line.speakerName.size() > MAX_ACTOR_NAME_BYTES || line.text.empty() ||
            line.text.size() > MAX_MEMORY_LINE_BYTES)
            return false;

        if (!PlayerbotSocialPublicIdIsValid(PlayerbotSocialIdKind::Event, line.sourceEventPublicId))
            return false;

        if (line.sourceKind != PlayerbotSocialMemorySourceKind::HumanObservation &&
            line.sourceKind != PlayerbotSocialMemorySourceKind::AuthoritativeSource)
            return false;

        for (std::size_t previous = 0; previous < index; ++previous)
        {
            if (request.thread[previous].sourceEventPublicId == line.sourceEventPublicId)
                return false;
        }
    }

    return true;
}

std::optional<std::string> PlayerbotLLM::SerializeMemoryRequest(MemoryRequest const& request,
                                                              std::string const& token)
{
    if (!MemoryRequestIsUsable(request, token))
        return std::nullopt;

    // Nested here, unlike the reply. This direction is only WRITTEN by the worldserver and read by
    // a schema-validating parser on the far side, so the flat-object constraint that shapes the
    // reply does not apply.
    std::string out;
    out.reserve(512 + request.thread.size() * 64);
    out += '{';
    AppendJsonField(out, "schema_version", SCHEMA_VERSION, true);
    AppendJsonField(out, "token", token);
    AppendJsonField(out, "kind", std::string(ResponseKindName(ResponseKind::Memory)));
    AppendJsonField(out, "memory_request_token", request.memoryRequestToken);
    AppendJsonField(out, "bot_guid", request.botGuidCounter);
    AppendJsonField(out, "bot_name", request.botName);
    AppendJsonField(out, "thread_id", request.threadPublicId);
    // Spelled here rather than borrowed from the repository's name helper, which lives behind a
    // header this module does not otherwise need. These three strings are a WIRE contract shared
    // with the sidecar's Literal, so they belong beside the frame that carries them.
    AppendJsonField(out, "scope",
                    std::string(request.scope == PlayerbotSocialPrivacyScope::Party ? "party" : "public"));

    out += ",\"subjects\":[";
    for (std::size_t index = 0; index < request.subjects.size(); ++index)
    {
        if (index != 0)
            out += ',';

        out += "{\"guid\":";
        out += std::to_string(request.subjects[index].guidCounter);
        out += ",\"name\":";
        AppendEscapedJsonString(out, request.subjects[index].name);
        out += '}';
    }
    out += ']';

    out += ",\"thread\":[";
    for (std::size_t index = 0; index < request.thread.size(); ++index)
    {
        if (index != 0)
            out += ',';

        MemoryLine const& line = request.thread[index];
        out += "{\"speaker_guid\":";
        out += std::to_string(line.speakerGuidCounter);
        out += ",\"speaker_name\":";
        AppendEscapedJsonString(out, line.speakerName);
        out += ",\"text\":";
        AppendEscapedJsonString(out, line.text);
        out += ",\"source_event_id\":";
        AppendEscapedJsonString(out, line.sourceEventPublicId);
        out += ",\"source_kind\":";
        AppendEscapedJsonString(out, std::string(PlayerbotSocialMemorySourceKindName(line.sourceKind)));
        out += '}';
    }
    out += "]}";

    return out;
}

std::optional<PlayerbotLLM::MemoryResponse> PlayerbotLLM::ParseMemoryResponsePayload(
    std::string const& payload, std::string const& expectedToken, uint64 expectedRequestToken,
    uint64 expectedBotGuidCounter)
{
    std::optional<std::map<std::string, FlatJsonValue>> fields = FlatJsonParser(payload).Parse();
    if (!fields)
        return std::nullopt;

    auto const schemaIt = fields->find("schema_version");
    auto const tokenIt = fields->find("token");
    auto const kindIt = fields->find("kind");
    auto const requestIt = fields->find("memory_request_token");
    auto const botIt = fields->find("bot_guid");
    auto const threadIt = fields->find("thread_id");
    auto const countIt = fields->find("memory_count");

    if (schemaIt == fields->end() || tokenIt == fields->end() || kindIt == fields->end() ||
        requestIt == fields->end() || botIt == fields->end() || threadIt == fields->end() ||
        countIt == fields->end())
        return std::nullopt;

    if (schemaIt->second.isString || schemaIt->second.number != SCHEMA_VERSION)
        return std::nullopt;

    if (!BridgeTokenIsUsable(expectedToken))
        return std::nullopt;

    if (!tokenIt->second.isString || !BridgeTokenIsUsable(tokenIt->second.text) ||
        !ConstantTimeEquals(tokenIt->second.text, expectedToken))
        return std::nullopt;

    // The declared kind is checked before anything is read out. A memory reply and a biography
    // reply share a token and a bot guid, and telling them apart by shape is how the wrong reader
    // gets handed the wrong frame.
    if (!kindIt->second.isString)
        return std::nullopt;

    std::optional<ResponseKind> const kind = ResponseKindFromName(kindIt->second.text);
    if (!kind || *kind != ResponseKind::Memory)
        return std::nullopt;

    // Identity before content: a well formed answer to a DIFFERENT request, or for a different
    // bot, is refused rather than handed to whoever is waiting.
    if (requestIt->second.isString || requestIt->second.number != expectedRequestToken)
        return std::nullopt;

    if (botIt->second.isString || botIt->second.number != expectedBotGuidCounter)
        return std::nullopt;

    if (!threadIt->second.isString || threadIt->second.text.empty() ||
        threadIt->second.text.size() > MAX_THREAD_ID_BYTES)
        return std::nullopt;

    if (countIt->second.isString || countIt->second.number > MAX_EXTRACTED_MEMORIES)
        return std::nullopt;

    std::size_t const count = static_cast<std::size_t>(countIt->second.number);

    /*
     * Seven protocol keys plus four per memory, counted exactly. The parser already refuses
     * duplicates, so an exact count is what refuses an unknown key, and here it also refuses a
     * payload whose slots disagree with its own declared count. Both are the same defect: the
     * reader and the writer do not agree about the frame, and reading it up to the count would
     * accept one carrying something nobody looked at.
     */
    if (fields->size() != 7 + count * 4)
        return std::nullopt;

    MemoryResponse response;
    response.memoryRequestToken = expectedRequestToken;
    response.botGuidCounter = expectedBotGuidCounter;
    response.threadPublicId = threadIt->second.text;
    response.memories.reserve(count);

    for (std::size_t index = 0; index < count; ++index)
    {
        std::string const slot = "memory_" + std::to_string(index) + "_";

        auto const paraphraseIt = fields->find(slot + "paraphrase");
        auto const aboutIt = fields->find(slot + "about_guid");
        auto const scopeIt = fields->find(slot + "scope");
        auto const sourceIt = fields->find(slot + "source_event_id");

        if (paraphraseIt == fields->end() || aboutIt == fields->end() || scopeIt == fields->end() ||
            sourceIt == fields->end())
            return std::nullopt;

        if (!paraphraseIt->second.isString || paraphraseIt->second.text.empty() ||
            paraphraseIt->second.text.size() > MAX_SOCIAL_CONTEXT_ENTRY_BYTES)
            return std::nullopt;

        if (aboutIt->second.isString || aboutIt->second.number == 0)
            return std::nullopt;

        if (!scopeIt->second.isString)
            return std::nullopt;

        if (!sourceIt->second.isString ||
            !PlayerbotSocialPublicIdIsValid(PlayerbotSocialIdKind::Event, sourceIt->second.text))
            return std::nullopt;

        MemoryResponseCandidate candidate;
        candidate.paraphrase = paraphraseIt->second.text;
        candidate.aboutGuidCounter = aboutIt->second.number;
        candidate.sourceEventPublicId = sourceIt->second.text;

        if (scopeIt->second.text == "public")
            candidate.scope = PlayerbotSocialPrivacyScope::Public;
        else if (scopeIt->second.text == "party")
            candidate.scope = PlayerbotSocialPrivacyScope::Party;
        else if (scopeIt->second.text == "whisper")
            candidate.scope = PlayerbotSocialPrivacyScope::Whisper;
        else
            return std::nullopt;

        response.memories.push_back(std::move(candidate));
    }

    return response;
}

// --- Roleplay assessment lane ---

char const* PlayerbotLLM::RoleplayContentCapabilityName(PlayerbotSocialContentCapability capability)
{
    using Capability = PlayerbotSocialContentCapability;

    switch (capability)
    {
        case Capability::ClassicContent:
            return "classic_content";
        case Capability::Outland:
            return "outland";
        case Capability::BloodElf:
            return "blood_elf";
        case Capability::Draenei:
            return "draenei";
        case Capability::DeathKnight:
            return "death_knight";
        case Capability::BurningCrusadeProfession:
            return "burning_crusade_profession";
        case Capability::WrathProfession:
            return "wrath_profession";
        case Capability::OtherBurningCrusade:
            return "other_burning_crusade";
        case Capability::OtherWrath:
            return "other_wrath";
        case Capability::Unknown:
            return "unknown";
    }

    return "invalid";
}

std::optional<PlayerbotSocialContentCapability> PlayerbotLLM::RoleplayContentCapabilityFromName(
    std::string const& name)
{
    using Capability = PlayerbotSocialContentCapability;

    // Exact match only, same as every other wire spelling in this protocol.
    if (name == "classic_content")
        return Capability::ClassicContent;
    if (name == "outland")
        return Capability::Outland;
    if (name == "blood_elf")
        return Capability::BloodElf;
    if (name == "draenei")
        return Capability::Draenei;
    if (name == "death_knight")
        return Capability::DeathKnight;
    if (name == "burning_crusade_profession")
        return Capability::BurningCrusadeProfession;
    if (name == "wrath_profession")
        return Capability::WrathProfession;
    if (name == "other_burning_crusade")
        return Capability::OtherBurningCrusade;
    if (name == "other_wrath")
        return Capability::OtherWrath;
    if (name == "unknown")
        return Capability::Unknown;

    return std::nullopt;
}

std::optional<PlayerbotRoleplayAssessmentKind> PlayerbotLLM::RoleplayAssessmentKindFromName(std::string const& name)
{
    if (name == "ordinary")
        return PlayerbotRoleplayAssessmentKind::Ordinary;
    if (name == "roleplay_invitation")
        return PlayerbotRoleplayAssessmentKind::RoleplayInvitation;
    if (name == "roleplay_continuation")
        return PlayerbotRoleplayAssessmentKind::RoleplayContinuation;
    if (name == "practical")
        return PlayerbotRoleplayAssessmentKind::Practical;
    if (name == "opt_out")
        return PlayerbotRoleplayAssessmentKind::OptOut;
    if (name == "uncertain")
        return PlayerbotRoleplayAssessmentKind::Uncertain;

    return std::nullopt;
}

bool PlayerbotLLM::RoleplayAssessmentRequestIsUsable(RoleplayAssessmentRequest const& request,
                                                   std::string const& token)
{
    if (!BridgeTokenIsUsable(token) || request.assessmentToken == 0)
        return false;

    if (request.threadPublicId.empty() || request.threadPublicId.size() > MAX_THREAD_ID_BYTES)
        return false;

    // Nothing to classify is not a request. The provider refuses it and the coordinator falls back
    // to ordinary activation rather than paying a round trip for an empty question.
    if (request.currentLine.empty() || request.currentLine.size() > MAX_SOCIAL_CONTEXT_ENTRY_BYTES)
        return false;

    if (request.threadLines.size() > MAX_SOCIAL_CONTEXT_ENTRIES)
        return false;

    for (std::string const& line : request.threadLines)
        if (line.empty() || line.size() > MAX_SOCIAL_CONTEXT_ENTRY_BYTES)
            return false;

    return true;
}

std::optional<std::string> PlayerbotLLM::SerializeRoleplayAssessmentRequest(RoleplayAssessmentRequest const& request,
                                                                          std::string const& token)
{
    if (!RoleplayAssessmentRequestIsUsable(request, token))
        return std::nullopt;

    std::string out;
    out.reserve(512 + request.currentLine.size() + request.threadLines.size() * 64);
    out += '{';
    AppendJsonField(out, "schema_version", SCHEMA_VERSION, true);
    AppendJsonField(out, "token", token);
    AppendJsonField(out, "kind", std::string(ResponseKindName(ResponseKind::RoleplayAssessment)));
    AppendJsonField(out, "roleplay_assessment_request_token", request.assessmentToken);
    AppendJsonField(out, "channel", static_cast<uint64>(request.channel));
    AppendJsonField(out, "thread_id", request.threadPublicId);
    AppendJsonField(out, "current_line", request.currentLine);

    // Nested here, like the memory thread: this direction is only written by the worldserver and
    // read by a schema validating parser on the far side.
    out += ",\"thread_lines\":[";
    for (std::size_t index = 0; index < request.threadLines.size(); ++index)
    {
        if (index != 0)
            out += ',';

        AppendEscapedJsonString(out, request.threadLines[index]);
    }
    out += "]}";

    return out;
}

std::optional<PlayerbotLLM::RoleplayAssessmentResponse> PlayerbotLLM::ParseRoleplayAssessmentResponsePayload(
    std::string const& payload, std::string const& expectedToken, uint64 expectedAssessmentToken)
{
    std::optional<std::map<std::string, FlatJsonValue>> fields = FlatJsonParser(payload).Parse();
    if (!fields)
        return std::nullopt;

    auto const schemaIt = fields->find("schema_version");
    auto const tokenIt = fields->find("token");
    auto const kindIt = fields->find("kind");
    auto const requestIt = fields->find("roleplay_assessment_request_token");
    auto const assessmentKindIt = fields->find("assessment_kind");
    auto const countIt = fields->find("capability_count");

    if (schemaIt == fields->end() || tokenIt == fields->end() || kindIt == fields->end() ||
        requestIt == fields->end() || assessmentKindIt == fields->end() || countIt == fields->end())
        return std::nullopt;

    if (schemaIt->second.isString || schemaIt->second.number != SCHEMA_VERSION)
        return std::nullopt;

    if (!BridgeTokenIsUsable(expectedToken))
        return std::nullopt;

    if (!tokenIt->second.isString || !BridgeTokenIsUsable(tokenIt->second.text) ||
        !ConstantTimeEquals(tokenIt->second.text, expectedToken))
        return std::nullopt;

    if (!kindIt->second.isString)
        return std::nullopt;

    std::optional<ResponseKind> const kind = ResponseKindFromName(kindIt->second.text);
    if (!kind || *kind != ResponseKind::RoleplayAssessment)
        return std::nullopt;

    // Identity before content: a well formed answer to a different assessment is refused.
    if (requestIt->second.isString || requestIt->second.number != expectedAssessmentToken)
        return std::nullopt;

    if (!assessmentKindIt->second.isString)
        return std::nullopt;

    std::optional<PlayerbotRoleplayAssessmentKind> const assessmentKind =
        RoleplayAssessmentKindFromName(assessmentKindIt->second.text);
    if (!assessmentKind)
        return std::nullopt;

    // Ten values is the whole vocabulary, so any larger count is malformed before its slots are read.
    if (countIt->second.isString || countIt->second.number > 10)
        return std::nullopt;

    std::size_t const count = static_cast<std::size_t>(countIt->second.number);

    // Six protocol keys plus one per capability, counted exactly: the parser already refuses
    // duplicate keys, so the exact count is what refuses unknown fields and mismatched slots.
    if (fields->size() != 6 + count)
        return std::nullopt;

    RoleplayAssessmentResponse response;
    response.assessmentToken = expectedAssessmentToken;
    response.kind = *assessmentKind;
    response.capabilities.reserve(count);

    for (std::size_t index = 0; index < count; ++index)
    {
        auto const capabilityIt = fields->find("capability_" + std::to_string(index));
        if (capabilityIt == fields->end() || !capabilityIt->second.isString)
            return std::nullopt;

        std::optional<PlayerbotSocialContentCapability> const capability =
            RoleplayContentCapabilityFromName(capabilityIt->second.text);
        if (!capability)
            return std::nullopt;

        response.capabilities.push_back(*capability);
    }

    // The per kind cardinality contract, enforced before any result reaches the coordinator.
    if (!PlayerbotSocialRoleplayAssessmentShapeIsValid(response.kind, response.capabilities))
        return std::nullopt;

    return response;
}

bool PlayerbotLLM::RoleplayAssessmentExchange::Open(uint64 assessmentToken, int64 expiresAtSteadyMs)
{
    if (assessmentToken == 0 || _deadlines.size() >= MAX_OUTSTANDING_ROLEPLAY_ASSESSMENTS)
        return false;

    return _deadlines.emplace(assessmentToken, expiresAtSteadyMs).second;
}

PlayerbotLLM::RoleplayAssessmentOutcome PlayerbotLLM::RoleplayAssessmentExchange::Settle(uint64 assessmentToken,
                                                                                    int64 nowMs)
{
    auto const found = _deadlines.find(assessmentToken);
    if (found == _deadlines.end())
        return RoleplayAssessmentOutcome::Abandon;

    int64 const expiresAtMs = found->second;
    _deadlines.erase(found);

    return nowMs <= expiresAtMs ? RoleplayAssessmentOutcome::Deliver : RoleplayAssessmentOutcome::Abandon;
}

std::vector<uint64> PlayerbotLLM::RoleplayAssessmentExchange::ExpireDue(int64 nowMs)
{
    std::vector<uint64> expired;
    for (auto it = _deadlines.begin(); it != _deadlines.end();)
    {
        if (nowMs > it->second)
        {
            expired.push_back(it->first);
            it = _deadlines.erase(it);
        }
        else
        {
            ++it;
        }
    }

    return expired;
}

std::vector<uint64> PlayerbotLLM::RoleplayAssessmentExchange::Clear()
{
    std::vector<uint64> outstanding;
    outstanding.reserve(_deadlines.size());
    for (auto const& [token, deadline] : _deadlines)
        outstanding.push_back(token);

    _deadlines.clear();
    return outstanding;
}

bool PlayerbotLLM::Bridge::TryEnqueueRoleplayAssessment(RoleplayAssessmentRequest request,
                                                            int64 expiresAtSteadyMs)
{
    if (_impl->stopped.load())
        return false;

    if (SteadyNowMs() > expiresAtSteadyMs)
        return false;

    return _impl->requests.TryPush(Impl::QueuedRequest{std::move(request), expiresAtSteadyMs});
}

std::vector<PlayerbotLLM::RoleplayAssessmentResponse> PlayerbotLLM::Bridge::DrainRoleplayAssessmentResponses()
{
    std::vector<RoleplayAssessmentResponse> drained;
    RoleplayAssessmentResponse response;
    while (_impl->assessmentResponses.TryPop(response))
        drained.push_back(std::move(response));

    return drained;
}
