/*
 * This file is part of the mod-playerbot-claude module.
 */

#include "ClaudeChat.h"

#include "gtest/gtest.h"

#include <boost/asio.hpp>

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <thread>
#include <vector>

using namespace ClaudeChat;

namespace
{
    std::string const TEST_TOKEN = "0123456789abcdef0123456789abcdef";  // 32 bytes

    ChatRequest MakeFixtureRequest()
    {
        ChatRequest request;
        request.requestId = 7;
        request.channel = ChatChannel::Whisper;
        request.botGuidCounter = 42;
        request.speakerGuidCounter = 9001;
        request.botName = "Botname";
        request.speakerName = "Speaker";
        request.profile.version = 2;
        request.profile.craftingAffinity = 65;
        request.profile.gatheringAffinity = 37;
        request.profile.explorationAffinity = 91;
        request.profile.sociability = 82;
        request.profile.voice = PlayerbotVoice::Earnest;
        request.message = "What do you enjoy doing?";
        request.eventKind = 0;
        request.subjectId = 0;
        request.occurrence = 0;
        request.expiresAtSteadyMs = SteadyNowMs() + 5000;
        return request;
    }

    std::string ValidResponsePayload(uint64 requestId, std::string const& message,
                                     std::string const& token = TEST_TOKEN, uint32 schemaVersion = 3)
    {
        return "{\"schema_version\":" + std::to_string(schemaVersion) + ",\"token\":\"" + token +
               "\",\"request_id\":" + std::to_string(requestId) + ",\"message\":\"" + message + "\"}";
    }

    // Polls a condition every 10 ms until it holds or the timeout expires.
    bool WaitFor(std::function<bool()> const& condition, int64 timeoutMs)
    {
        int64 const start = SteadyNowMs();
        while (SteadyNowMs() - start < timeoutMs)
        {
            if (condition())
                return true;

            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return condition();
    }

    // Minimal blocking loopback server speaking the frame protocol. Each accepted
    // connection reads exactly one request frame and invokes the handler; an empty
    // handler result closes the connection without replying.
    class FakeSidecarServer
    {
    public:
        using Responder = std::function<std::optional<std::string>(std::string const& requestPayload)>;

        explicit FakeSidecarServer(Responder responder)
            : _acceptor(_io, boost::asio::ip::tcp::endpoint(boost::asio::ip::make_address("127.0.0.1"), 0)),
              _responder(std::move(responder))
        {
            _port = _acceptor.local_endpoint().port();
            _thread = std::thread([this]() { Run(); });
        }

        ~FakeSidecarServer()
        {
            _stopping = true;
            boost::system::error_code ec;
            _acceptor.close(ec);
            if (_thread.joinable())
                _thread.join();
        }

        uint16_t Port() const { return _port; }
        uint32_t HandledRequests() const { return _handled.load(); }

    private:
        void Run()
        {
            while (!_stopping)
            {
                boost::asio::ip::tcp::socket socket(_io);
                boost::system::error_code ec;
                _acceptor.accept(socket, ec);
                if (ec)
                    return;

                HandleConnection(socket);
            }
        }

        void HandleConnection(boost::asio::ip::tcp::socket& socket)
        {
            boost::system::error_code ec;
            while (!_stopping)
            {
                std::array<unsigned char, 4> header{};
                boost::asio::read(socket, boost::asio::buffer(header), ec);
                if (ec)
                    return;

                uint32_t const length = (uint32_t(header[0]) << 24) | (uint32_t(header[1]) << 16) |
                                        (uint32_t(header[2]) << 8) | uint32_t(header[3]);
                std::string payload(length, '\0');
                boost::asio::read(socket, boost::asio::buffer(payload.data(), payload.size()), ec);
                if (ec)
                    return;

                ++_handled;
                std::optional<std::string> const reply = _responder(payload);
                if (!reply)
                {
                    socket.close(ec);
                    return;
                }

                std::optional<std::vector<uint8>> const frame = EncodeFrame(*reply);
                ASSERT_TRUE(frame.has_value());
                boost::asio::write(socket, boost::asio::buffer(*frame), ec);
                if (ec)
                    return;
            }
        }

        boost::asio::io_context _io;
        boost::asio::ip::tcp::acceptor _acceptor;
        Responder _responder;
        std::thread _thread;
        std::atomic<bool> _stopping{false};
        std::atomic<uint32_t> _handled{0};
        uint16_t _port = 0;
    };

    BridgeConfig MakeBridgeConfig(uint16_t port, uint32 queueCapacity = 8)
    {
        BridgeConfig config;
        config.port = port;
        config.token = TEST_TOKEN;
        config.queueCapacity = queueCapacity;
        config.socketTimeoutMs = 500;
        return config;
    }
}

// --- Protocol: framing ---

TEST(ClaudeChatProtocolTest, FrameEncodesBigEndianLengthPrefix)
{
    std::optional<std::vector<uint8>> const frame = EncodeFrame("abc");
    ASSERT_TRUE(frame.has_value());
    ASSERT_EQ(frame->size(), 7u);
    EXPECT_EQ((*frame)[0], 0u);
    EXPECT_EQ((*frame)[1], 0u);
    EXPECT_EQ((*frame)[2], 0u);
    EXPECT_EQ((*frame)[3], 3u);
    EXPECT_EQ((*frame)[4], 'a');
    EXPECT_EQ((*frame)[6], 'c');
}

TEST(ClaudeChatProtocolTest, FrameRejectsOversizedPayload)
{
    std::string const oversized(MAX_FRAME_PAYLOAD_BYTES + 1, 'x');
    EXPECT_FALSE(EncodeFrame(oversized).has_value());

    std::string const atLimit(MAX_FRAME_PAYLOAD_BYTES, 'x');
    EXPECT_TRUE(EncodeFrame(atLimit).has_value());
}

TEST(ClaudeChatProtocolTest, FrameLengthDecodeRejectsOversizedLength)
{
    std::array<uint8, FRAME_HEADER_BYTES> valid{0x00, 0x00, 0x00, 0x10};
    ASSERT_TRUE(DecodeFrameLength(valid).has_value());
    EXPECT_EQ(*DecodeFrameLength(valid), 16u);

    // 64 KiB + 1
    std::array<uint8, FRAME_HEADER_BYTES> oversized{0x00, 0x01, 0x00, 0x01};
    EXPECT_FALSE(DecodeFrameLength(oversized).has_value());
}

// --- Protocol: request serialization ---

TEST(ClaudeChatProtocolTest, RequestSerializesToExactContractJson)
{
    std::string const expected =
        "{\"schema_version\":3,"
        "\"token\":\"0123456789abcdef0123456789abcdef\","
        "\"request_id\":7,"
        "\"channel\":\"whisper\","
        "\"bot_guid\":42,"
        "\"speaker_guid\":9001,"
        "\"bot_name\":\"Botname\","
        "\"speaker_name\":\"Speaker\","
        "\"profile_version\":2,"
        "\"crafting_affinity\":65,"
        "\"gathering_affinity\":37,"
        "\"exploration_affinity\":91,"
        "\"sociability\":82,"
        "\"voice\":\"earnest\","
        "\"event_kind\":0,"
        "\"subject_id\":0,"
        "\"occurrence\":0,"
        "\"message\":\"What do you enjoy doing?\"}";

    EXPECT_EQ(SerializeRequest(MakeFixtureRequest(), TEST_TOKEN), expected);
}

TEST(ClaudeChatProtocolTest, RequestSerializationEscapesUntrustedText)
{
    ChatRequest request = MakeFixtureRequest();
    request.message = "say \"hi\" \\ and\nrun\tfast \x01 caf\xC3\xA9";

    std::string const serialized = SerializeRequest(request, TEST_TOKEN).value();
    EXPECT_NE(serialized.find("say \\\"hi\\\" \\\\ and\\nrun\\tfast \\u0001 caf\xC3\xA9"), std::string::npos);
}

TEST(ClaudeChatProtocolTest, PartyChannelSerializesAsParty)
{
    ChatRequest request = MakeFixtureRequest();
    request.channel = ChatChannel::Party;
    EXPECT_NE(SerializeRequest(request, TEST_TOKEN).value().find("\"channel\":\"party\""), std::string::npos);
}

TEST(ClaudeChatProtocolTest, AmbientRequestSerializesToExactContractJson)
{
    ChatRequest request = MakeFixtureRequest();
    request.requestId = 8;
    request.channel = ChatChannel::World;
    request.speakerGuidCounter = 0;
    request.speakerName.clear();
    request.message = AMBIENT_EVENT_MARKER;
    request.eventKind = AMBIENT_EVENT_KIND;
    request.occurrence = 9;

    std::string const expected =
        "{\"schema_version\":3,"
        "\"token\":\"0123456789abcdef0123456789abcdef\","
        "\"request_id\":8,"
        "\"channel\":\"world\","
        "\"bot_guid\":42,"
        "\"speaker_guid\":0,"
        "\"bot_name\":\"Botname\","
        "\"speaker_name\":\"\","
        "\"profile_version\":2,"
        "\"crafting_affinity\":65,"
        "\"gathering_affinity\":37,"
        "\"exploration_affinity\":91,"
        "\"sociability\":82,"
        "\"voice\":\"earnest\","
        "\"event_kind\":4,"
        "\"subject_id\":0,"
        "\"occurrence\":9,"
        "\"message\":\"ambient_world\"}";

    EXPECT_EQ(SerializeRequest(request, TEST_TOKEN).value(), expected);
}

TEST(ClaudeChatProtocolTest, CareerRequestUsesOpaqueCandidates)
{
    PlayerbotCareerPlanRequest request;
    request.personalityVersion = 2u;
    request.careerVersion = 1u;
    request.candidates =
    {
        { "career-none", "no professions", PlayerbotRecipeSpendingStyle::None, false, 0u },
        { "career-deadbeef", "mixed primary professions",
          PlayerbotRecipeSpendingStyle::Completionist, true, 90u }
    };

    EXPECT_EQ(
        SerializeCareerRequestContent(request).value(),
        "{\"personality_version\":2,\"career_version\":1,\"candidates\":["
        "{\"token\":\"career-none\",\"summary\":\"no professions\","
        "\"maximum_spending_style\":\"none\",\"market_eligible\":0,\"engagement\":0},"
        "{\"token\":\"career-deadbeef\",\"summary\":\"mixed primary professions\","
        "\"maximum_spending_style\":\"completionist\",\"market_eligible\":1,\"engagement\":90}]}");
}

TEST(ClaudeChatProtocolTest, CareerDecisionParserIsStrict)
{
    std::optional<CareerDecision> const valid =
        ParseCareerDecision("{\"candidate_token\":\"career-deadbeef\",\"spending_style\":\"progression\"}");
    ASSERT_TRUE(valid);
    EXPECT_EQ(valid->candidateToken, "career-deadbeef");
    EXPECT_EQ(valid->spendingStyle, PlayerbotRecipeSpendingStyle::Progression);

    EXPECT_FALSE(ParseCareerDecision(
        "{\"candidate_token\":\"career-deadbeef\",\"spending_style\":\"invalid\"}"));
    EXPECT_FALSE(ParseCareerDecision(
        "{\"candidate_token\":\"career-deadbeef\",\"spending_style\":\"minimal\",\"raw_skill_id\":171}"));
}

// --- Protocol: response parsing ---

TEST(ClaudeChatProtocolTest, ValidResponseRoundTrips)
{
    std::optional<ChatResponse> const response =
        ParseResponsePayload(ValidResponsePayload(7, "I enjoy fishing."), TEST_TOKEN);
    ASSERT_TRUE(response.has_value());
    EXPECT_EQ(response->requestId, 7u);
    EXPECT_EQ(response->message, "I enjoy fishing.");
}

TEST(ClaudeChatProtocolTest, ResponseUnescapesMessage)
{
    std::optional<ChatResponse> const response =
        ParseResponsePayload(ValidResponsePayload(7, "quote \\\" backslash \\\\ done"), TEST_TOKEN);
    ASSERT_TRUE(response.has_value());
    EXPECT_EQ(response->message, "quote \" backslash \\ done");
}

TEST(ClaudeChatProtocolTest, ResponseRejectsWrongSchemaVersion)
{
    EXPECT_FALSE(ParseResponsePayload(ValidResponsePayload(7, "hello", TEST_TOKEN, 1), TEST_TOKEN).has_value());
}

TEST(ClaudeChatProtocolTest, ResponseRejectsWrongToken)
{
    std::string const wrongToken(32, 'z');
    EXPECT_FALSE(ParseResponsePayload(ValidResponsePayload(7, "hello", wrongToken), TEST_TOKEN).has_value());
}

TEST(ClaudeChatProtocolTest, ResponseRejectsMalformedJson)
{
    EXPECT_FALSE(ParseResponsePayload("", TEST_TOKEN).has_value());
    EXPECT_FALSE(ParseResponsePayload("not json", TEST_TOKEN).has_value());
    EXPECT_FALSE(ParseResponsePayload("{\"schema_version\":1", TEST_TOKEN).has_value());
    EXPECT_FALSE(ParseResponsePayload("[1,2,3]", TEST_TOKEN).has_value());
}

TEST(ClaudeChatProtocolTest, ResponseRejectsMissingOrExtraFields)
{
    // Missing message.
    EXPECT_FALSE(ParseResponsePayload("{\"schema_version\":1,\"token\":\"" + TEST_TOKEN +
                                          "\",\"request_id\":7}",
                                      TEST_TOKEN)
                     .has_value());

    // Extra field.
    EXPECT_FALSE(ParseResponsePayload("{\"schema_version\":1,\"token\":\"" + TEST_TOKEN +
                                          "\",\"request_id\":7,\"message\":\"m\",\"action\":\"cast\"}",
                                      TEST_TOKEN)
                     .has_value());

    // Duplicate field.
    EXPECT_FALSE(ParseResponsePayload("{\"schema_version\":1,\"token\":\"" + TEST_TOKEN +
                                          "\",\"request_id\":7,\"request_id\":8,\"message\":\"m\"}",
                                      TEST_TOKEN)
                     .has_value());
}

TEST(ClaudeChatProtocolTest, ResponseRejectsOversizedOrMultilineMessage)
{
    std::string const oversized(MAX_RESPONSE_MESSAGE_BYTES + 1, 'a');
    EXPECT_FALSE(ParseResponsePayload(ValidResponsePayload(7, oversized), TEST_TOKEN).has_value());

    std::string const atLimit(MAX_RESPONSE_MESSAGE_BYTES, 'a');
    EXPECT_TRUE(ParseResponsePayload(ValidResponsePayload(7, atLimit), TEST_TOKEN).has_value());

    EXPECT_FALSE(ParseResponsePayload(ValidResponsePayload(7, "two\\nlines"), TEST_TOKEN).has_value());
}

TEST(ClaudeChatProtocolTest, ResponseRejectsInvalidUtf8)
{
    EXPECT_FALSE(ParseResponsePayload(ValidResponsePayload(7, "bad \xFF byte"), TEST_TOKEN).has_value());
}

TEST(ClaudeChatProtocolTest, Utf8TruncationNeverSplitsASequence)
{
    EXPECT_EQ(TruncateUtf8Bytes("hello", 10), "hello");
    EXPECT_EQ(TruncateUtf8Bytes("hello", 5), "hello");
    EXPECT_EQ(TruncateUtf8Bytes("hello", 4), "hell");

    // "café" is 5 bytes (0xC3 0xA9 for the accented letter): cutting at 4 bytes must
    // drop the whole 2-byte sequence, not half of it.
    std::string const cafe = "caf\xC3\xA9";
    EXPECT_EQ(TruncateUtf8Bytes(cafe, 5), cafe);
    EXPECT_EQ(TruncateUtf8Bytes(cafe, 4), "caf");

    // Already-invalid input is cut at its first invalid byte.
    EXPECT_EQ(TruncateUtf8Bytes("ok\xFFtail", 10), "ok");
}

// --- Token acquisition ---

TEST(ClaudeChatProtocolTest, BridgeTokenFailsClosed)
{
    unsetenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN");
    EXPECT_FALSE(BridgeTokenFromEnvironment().has_value());

    setenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN", "too-short", 1);
    EXPECT_FALSE(BridgeTokenFromEnvironment().has_value());

    setenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN", TEST_TOKEN.c_str(), 1);
    std::optional<std::string> const token = BridgeTokenFromEnvironment();
    ASSERT_TRUE(token.has_value());
    EXPECT_EQ(*token, TEST_TOKEN);
    unsetenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN");
}

// --- Queue ---

TEST(ClaudeChatQueueTest, FullQueueRejectsImmediately)
{
    BoundedQueue<int> queue(2);
    EXPECT_TRUE(queue.TryPush(1));
    EXPECT_TRUE(queue.TryPush(2));
    EXPECT_FALSE(queue.TryPush(3));

    int value = 0;
    EXPECT_TRUE(queue.TryPop(value));
    EXPECT_EQ(value, 1);
    EXPECT_TRUE(queue.TryPush(3));
}

TEST(ClaudeChatQueueTest, StoppedQueueRejectsPushAndPop)
{
    BoundedQueue<int> queue(2);
    EXPECT_TRUE(queue.TryPush(1));
    queue.Stop();
    EXPECT_FALSE(queue.TryPush(2));

    int value = 0;
    EXPECT_FALSE(queue.TryPop(value));
}

TEST(ClaudeChatQueueTest, ExpiredRequestIsRejectedAtEnqueue)
{
    ClaudeBridge bridge(MakeBridgeConfig(1));  // port unused: never started

    ChatRequest request = MakeFixtureRequest();
    request.expiresAtSteadyMs = SteadyNowMs() - 1;
    EXPECT_FALSE(bridge.TryEnqueue(request));
}

TEST(ClaudeChatQueueTest, StoppedBridgeRejectsEnqueue)
{
    ClaudeBridge bridge(MakeBridgeConfig(1));
    bridge.Start();
    bridge.Stop();
    EXPECT_FALSE(bridge.TryEnqueue(MakeFixtureRequest()));
}

// --- Bridge worker ---

TEST(ClaudeChatBridgeTest, RoundTripDeliversResponse)
{
    FakeSidecarServer server([](std::string const& payload) -> std::optional<std::string>
    {
        EXPECT_NE(payload.find("\"request_id\":7"), std::string::npos);
        EXPECT_NE(payload.find("\"token\":\"" + TEST_TOKEN + "\""), std::string::npos);
        EXPECT_NE(payload.find("\"crafting_affinity\":65"), std::string::npos);
        return ValidResponsePayload(7, "I enjoy fishing.");
    });

    ClaudeBridge bridge(MakeBridgeConfig(server.Port()));
    bridge.Start();
    ASSERT_TRUE(bridge.TryEnqueue(MakeFixtureRequest()));

    std::vector<ChatResponse> responses;
    ASSERT_TRUE(WaitFor([&]()
    {
        std::vector<ChatResponse> drained = bridge.DrainResponses();
        responses.insert(responses.end(), drained.begin(), drained.end());
        return !responses.empty();
    }, 5000));

    bridge.Stop();

    ASSERT_EQ(responses.size(), 1u);
    EXPECT_EQ(responses[0].requestId, 7u);
    EXPECT_EQ(responses[0].message, "I enjoy fishing.");
}

TEST(ClaudeChatBridgeTest, WrongTokenResponseIsDropped)
{
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string>
    {
        return ValidResponsePayload(7, "I enjoy fishing.", std::string(32, 'z'));
    });

    ClaudeBridge bridge(MakeBridgeConfig(server.Port()));
    bridge.Start();
    ASSERT_TRUE(bridge.TryEnqueue(MakeFixtureRequest()));

    // Wait until the server has handled the request, then confirm nothing is delivered.
    ASSERT_TRUE(WaitFor([&]() { return server.HandledRequests() >= 1; }, 5000));
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    EXPECT_TRUE(bridge.DrainResponses().empty());

    bridge.Stop();
}

TEST(ClaudeChatBridgeTest, StopDoesNotWaitForSilentServer)
{
    std::atomic<bool> release{false};
    FakeSidecarServer server([&release](std::string const&) -> std::optional<std::string>
    {
        // Stay silent until the test releases the handler (bounded at 30 s).
        for (int i = 0; i < 3000 && !release.load(); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        return std::nullopt;
    });

    ClaudeBridge bridge(MakeBridgeConfig(server.Port()));
    bridge.Start();
    ASSERT_TRUE(bridge.TryEnqueue(MakeFixtureRequest()));
    ASSERT_TRUE(WaitFor([&]() { return server.HandledRequests() >= 1; }, 5000));

    int64 const stopStart = SteadyNowMs();
    bridge.Stop();
    EXPECT_LT(SteadyNowMs() - stopStart, 2000);

    release = true;  // let the server handler exit so teardown does not wait
}

// --- Milestone speaker selection ---

TEST(ClaudeChatSelectionTest, LiteralFixturesMatchContract)
{
    // Hard-coded expected selections from the approved plan; never recomputed through
    // the production helper.
    MilestoneEventId quest{1, 9001, 12345, 0};
    EXPECT_EQ(SelectMilestoneSpeaker(quest, {{10, 0}, {20, 100}}), std::optional<uint64>(20));

    MilestoneEventId level{2, 9001, 80, 1};
    EXPECT_EQ(SelectMilestoneSpeaker(level, {{10, 0}, {20, 100}}), std::optional<uint64>(20));

    MilestoneEventId loot{3, 9001, 19019, 2};
    EXPECT_EQ(SelectMilestoneSpeaker(loot, {{30, 34}, {10, 91}, {20, 8}}), std::optional<uint64>(10));

    MilestoneEventId smallQuest{1, 42, 7, 0};
    EXPECT_EQ(SelectMilestoneSpeaker(smallQuest, {{10, 50}, {20, 50}, {30, 50}}), std::optional<uint64>(30));
}

TEST(ClaudeChatSelectionTest, ReorderingCandidatesDoesNotChangeSelection)
{
    MilestoneEventId loot{3, 9001, 19019, 2};
    std::optional<uint64> const sortedOrder = SelectMilestoneSpeaker(loot, {{10, 91}, {20, 8}, {30, 34}});
    std::optional<uint64> const shuffled = SelectMilestoneSpeaker(loot, {{30, 34}, {20, 8}, {10, 91}});
    EXPECT_EQ(sortedOrder, shuffled);
    EXPECT_EQ(sortedOrder, std::optional<uint64>(10));
}

TEST(ClaudeChatSelectionTest, EveryCandidateRetainsNonzeroChance)
{
    // Even a zero-sociability candidate has weight 1. Across many occurrences of the
    // same milestone kind every candidate must win at least once.
    bool seen10 = false;
    bool seen20 = false;
    bool seen30 = false;
    for (uint64 occurrence = 0; occurrence < 1000; ++occurrence)
    {
        MilestoneEventId eventId{1, 9001, 12345, occurrence};
        std::optional<uint64> const selected =
            SelectMilestoneSpeaker(eventId, {{10, 0}, {20, 100}, {30, 0}});
        ASSERT_TRUE(selected.has_value());
        seen10 = seen10 || *selected == 10;
        seen20 = seen20 || *selected == 20;
        seen30 = seen30 || *selected == 30;
    }
    EXPECT_TRUE(seen10);
    EXPECT_TRUE(seen20);
    EXPECT_TRUE(seen30);
}

TEST(ClaudeChatSelectionTest, EmptyCandidateListSelectsNothing)
{
    MilestoneEventId quest{1, 9001, 12345, 0};
    EXPECT_FALSE(SelectMilestoneSpeaker(quest, {}).has_value());
}

TEST(ClaudeChatSelectionTest, AmbientLiteralFixturesMatchContract)
{
    std::vector<SpeakerCandidate> candidates{{30, 0}, {10, 0}, {20, 0}};
    EXPECT_EQ(SelectAmbientSpeaker(0, candidates), std::optional<uint64>(30));
    EXPECT_EQ(SelectAmbientSpeaker(1, candidates), std::optional<uint64>(20));
    EXPECT_EQ(SelectAmbientSpeaker(2, candidates), std::optional<uint64>(10));
}

TEST(ClaudeChatSelectionTest, AmbientSelectionIsStableAcrossCandidateOrder)
{
    std::optional<uint64> const sorted = SelectAmbientSpeaker(17, {{10, 91}, {20, 8}, {30, 34}});
    std::optional<uint64> const shuffled = SelectAmbientSpeaker(17, {{30, 34}, {10, 91}, {20, 8}});
    EXPECT_EQ(sorted, shuffled);
    EXPECT_EQ(sorted, std::optional<uint64>(10));
    EXPECT_FALSE(SelectAmbientSpeaker(17, {}).has_value());
}

TEST(ClaudeChatPolicyTest, AmbientCadenceHasNoStartupOrCatchUpBurst)
{
    AmbientCadence cadence(6, 1000);
    EXPECT_TRUE(cadence.IsValid());
    EXPECT_FALSE(cadence.TryConsumeDueSlot(1000));
    EXPECT_FALSE(cadence.TryConsumeDueSlot(600999));
    EXPECT_TRUE(cadence.TryConsumeDueSlot(601000));
    EXPECT_FALSE(cadence.TryConsumeDueSlot(601000));

    EXPECT_TRUE(cadence.TryConsumeDueSlot(3601000));
    EXPECT_FALSE(cadence.TryConsumeDueSlot(3601000));
    EXPECT_FALSE(cadence.TryConsumeDueSlot(4200999));
    EXPECT_TRUE(cadence.TryConsumeDueSlot(4201000));
}

TEST(ClaudeChatPolicyTest, AmbientCadenceRejectsRatesOutsideHardLimit)
{
    EXPECT_FALSE(AmbientCadence(0, 0).IsValid());
    EXPECT_TRUE(AmbientCadence(1, 0).IsValid());
    EXPECT_TRUE(AmbientCadence(MAX_AMBIENT_MESSAGES_PER_HOUR, 0).IsValid());
    EXPECT_FALSE(AmbientCadence(MAX_AMBIENT_MESSAGES_PER_HOUR + 1, 0).IsValid());
}

TEST(ClaudeChatPolicyTest, AmbientEligibilityRequiresHumanAndAvailableQuietBot)
{
    AmbientCandidateSnapshot good;
    good.botOnline = true;
    good.botAlive = true;
    good.botIsMachine = true;
    good.botInCombat = false;
    good.worldChannelAvailable = true;

    EXPECT_TRUE(ShouldEnqueueAmbient(true, {good}));
    EXPECT_FALSE(ShouldEnqueueAmbient(false, {good}));
    EXPECT_FALSE(ShouldEnqueueAmbient(true, {}));

    AmbientCandidateSnapshot noChannel = good;
    noChannel.worldChannelAvailable = false;
    EXPECT_FALSE(ShouldEnqueueAmbient(true, {noChannel}));

    AmbientCandidateSnapshot fighting = good;
    fighting.botInCombat = true;
    EXPECT_FALSE(ShouldEnqueueAmbient(true, {fighting}));
}

TEST(ClaudeChatPolicyTest, TheLegacyAmbientLimiterYieldsToTheInteractiveSocialFeature)
{
    /*
     * The hourly limiter and the interactive social feature are two answers to the same question, so
     * only one of them may be speaking. While the social gate is on, social density, probability
     * decay, cooldowns, and budget admission decide when a bot opens its mouth, and this limiter
     * steps aside entirely rather than adding an unrelated World line on top.
     */
    EXPECT_TRUE(LegacyAmbientWorldAllowed(true, false));
    EXPECT_FALSE(LegacyAmbientWorldAllowed(true, true));

    // And the gate being on is not a way to turn the limiter on: an unconfigured limiter stays off.
    EXPECT_FALSE(LegacyAmbientWorldAllowed(false, false));
    EXPECT_FALSE(LegacyAmbientWorldAllowed(false, true));
}

TEST(ClaudeChatBridgeTest, DeclinedAmbientSnapshotProducesNoOutboundFrame)
{
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string>
    {
        return ValidResponsePayload(1, "unexpected");
    });

    ClaudeBridge bridge(MakeBridgeConfig(server.Port()));
    bridge.Start();

    ASSERT_FALSE(ShouldEnqueueAmbient(false, {}));
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    EXPECT_EQ(server.HandledRequests(), 0u);
    bridge.Stop();
}

TEST(ClaudeChatSelectionTest, ExactDuplicateEventIdCannotEnqueueTwice)
{
    RecentEventIdSet recent(8);
    MilestoneEventId quest{1, 9001, 12345, 0};
    EXPECT_TRUE(recent.Insert(quest));
    EXPECT_FALSE(recent.Insert(quest));

    // A later occurrence is a distinct event.
    MilestoneEventId next{1, 9001, 12345, 1};
    EXPECT_TRUE(recent.Insert(next));
}

TEST(ClaudeChatSelectionTest, RecentEventIdSetIsBounded)
{
    RecentEventIdSet recent(2);
    MilestoneEventId first{1, 9001, 1, 0};
    MilestoneEventId second{1, 9001, 2, 0};
    MilestoneEventId third{1, 9001, 3, 0};
    EXPECT_TRUE(recent.Insert(first));
    EXPECT_TRUE(recent.Insert(second));
    EXPECT_TRUE(recent.Insert(third));   // evicts first
    EXPECT_TRUE(recent.Insert(first));   // first was evicted, insertable again
    EXPECT_FALSE(recent.Insert(third));  // still tracked
}

// --- Chat capture parsing and delivery policy ---

TEST(ClaudeChatPolicyTest, WhisperLlmSyntaxIsExplicit)
{
    EXPECT_EQ(ParseLlmWhisper("llm What do you enjoy doing?"),
              std::optional<std::string>("What do you enjoy doing?"));
    EXPECT_FALSE(ParseLlmWhisper("llm").has_value());
    EXPECT_FALSE(ParseLlmWhisper("llm ").has_value());
    EXPECT_FALSE(ParseLlmWhisper("LLM hello").has_value());
    EXPECT_FALSE(ParseLlmWhisper(" llm hello").has_value());
    EXPECT_FALSE(ParseLlmWhisper("tell me about llm hello").has_value());
    EXPECT_FALSE(ParseLlmWhisper("llmhello").has_value());
}

TEST(ClaudeChatPolicyTest, WhisperRoutingPrefersCommandsOverClaude)
{
    // Explicit llm prefix always routes to Claude, even for command-shaped text.
    EXPECT_EQ(WhisperClaudeText("llm follow", true), std::optional<std::string>("follow"));
    EXPECT_EQ(WhisperClaudeText("llm What do you enjoy doing?", false),
              std::optional<std::string>("What do you enjoy doing?"));

    // Unprefixed text goes to Claude only when it is NOT a known playerbot command.
    EXPECT_EQ(WhisperClaudeText("What do you enjoy doing?", false),
              std::optional<std::string>("What do you enjoy doing?"));
    EXPECT_FALSE(WhisperClaudeText("follow", true).has_value());
    EXPECT_FALSE(WhisperClaudeText("grind loot", true).has_value());

    // Degenerate input never routes.
    EXPECT_FALSE(WhisperClaudeText("", false).has_value());
    EXPECT_FALSE(WhisperClaudeText("   ", false).has_value());
    EXPECT_FALSE(WhisperClaudeText("llm ", false).has_value());
}

TEST(ClaudeChatPolicyTest, PartyLlmSyntaxNamesExactlyOneBot)
{
    std::optional<std::pair<std::string, std::string>> const parsed =
        ParseLlmParty("llm Botname What do you think?");
    ASSERT_TRUE(parsed.has_value());
    EXPECT_EQ(parsed->first, "Botname");
    EXPECT_EQ(parsed->second, "What do you think?");

    EXPECT_FALSE(ParseLlmParty("llm Botname").has_value());
    EXPECT_FALSE(ParseLlmParty("llm Botname ").has_value());
    EXPECT_FALSE(ParseLlmParty("llm").has_value());
    EXPECT_FALSE(ParseLlmParty("hello llm Botname hi").has_value());
}

TEST(ClaudeChatPolicyTest, WhisperDeliveryRequiresValidRevalidatedState)
{
    DeliverySnapshot good;
    good.botOnline = true;
    good.speakerOnline = true;
    good.botIsStillBot = true;
    good.botInCombat = false;
    good.sameGroup = false;  // irrelevant for whispers
    good.expired = false;
    EXPECT_TRUE(ShouldDeliver(ChatChannel::Whisper, good));

    // A response drained after its deadline must never be spoken, even when
    // every other revalidated field still passes.
    DeliverySnapshot expired = good;
    expired.expired = true;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::Whisper, expired));

    // Every field of a default snapshot sits on the blocking side.
    EXPECT_FALSE(ShouldDeliver(ChatChannel::Whisper, DeliverySnapshot{}));

    DeliverySnapshot botOffline = good;
    botOffline.botOnline = false;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::Whisper, botOffline));

    DeliverySnapshot speakerOffline = good;
    speakerOffline.speakerOnline = false;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::Whisper, speakerOffline));

    DeliverySnapshot notBotAnymore = good;
    notBotAnymore.botIsStillBot = false;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::Whisper, notBotAnymore));

    // Players whisper mid-fight all the time; a fighting bot still replies.
    DeliverySnapshot inCombat = good;
    inCombat.botInCombat = true;
    EXPECT_TRUE(ShouldDeliver(ChatChannel::Whisper, inCombat));
}

TEST(ClaudeChatPolicyTest, PartyDeliveryAlsoRequiresSameGroup)
{
    DeliverySnapshot good;
    good.botOnline = true;
    good.speakerOnline = true;
    good.botIsStillBot = true;
    good.botInCombat = false;
    good.sameGroup = true;
    good.expired = false;
    EXPECT_TRUE(ShouldDeliver(ChatChannel::Party, good));

    DeliverySnapshot leftGroup = good;
    leftGroup.sameGroup = false;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::Party, leftGroup));

    // Party milestone reactions stay quiet during a fight (unlike whispers).
    DeliverySnapshot inCombat = good;
    inCombat.botInCombat = true;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::Party, inCombat));
}

TEST(ClaudeChatPolicyTest, WorldDeliveryRequiresHumanAndQuietAvailableBot)
{
    DeliverySnapshot good;
    good.botOnline = true;
    good.botAlive = true;
    good.botIsStillBot = true;
    good.botInCombat = false;
    good.humanOnline = true;
    good.worldChannelAvailable = true;
    good.expired = false;
    EXPECT_TRUE(ShouldDeliver(ChatChannel::World, good));

    DeliverySnapshot noHuman = good;
    noHuman.humanOnline = false;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::World, noHuman));

    DeliverySnapshot dead = good;
    dead.botAlive = false;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::World, dead));

    DeliverySnapshot fighting = good;
    fighting.botInCombat = true;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::World, fighting));

    DeliverySnapshot noChannel = good;
    noChannel.worldChannelAvailable = false;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::World, noChannel));
}

TEST(ClaudeChatPolicyTest, GroupCooldownAllowsOneMilestonePerWindow)
{
    GroupCooldownTracker cooldowns;
    int64 const cooldownMs = 120000;

    EXPECT_TRUE(cooldowns.TryBegin(5, 1000, cooldownMs));
    EXPECT_FALSE(cooldowns.TryBegin(5, 1000 + cooldownMs - 1, cooldownMs));
    EXPECT_TRUE(cooldowns.TryBegin(5, 1000 + cooldownMs, cooldownMs));

    // A different group is unaffected.
    EXPECT_TRUE(cooldowns.TryBegin(6, 1000, cooldownMs));
}

TEST(ClaudeChatBridgeTest, ReconnectsAfterServerClosesConnection)
{
    std::atomic<uint32_t> requestCounter{0};
    FakeSidecarServer server([&requestCounter](std::string const& payload) -> std::optional<std::string>
    {
        uint32_t const index = ++requestCounter;
        if (index == 1)
            return std::nullopt;  // close without replying: request 1 is lost, fail closed

        size_t const idPos = payload.find("\"request_id\":");
        uint64_t requestId = std::stoull(payload.substr(idPos + 13));
        return ValidResponsePayload(requestId, "back again");
    });

    ClaudeBridge bridge(MakeBridgeConfig(server.Port()));
    bridge.Start();

    ChatRequest first = MakeFixtureRequest();
    first.requestId = 21;
    ASSERT_TRUE(bridge.TryEnqueue(first));
    ASSERT_TRUE(WaitFor([&]() { return requestCounter.load() >= 1; }, 5000));

    ChatRequest second = MakeFixtureRequest();
    second.requestId = 22;
    second.expiresAtSteadyMs = SteadyNowMs() + 10000;
    ASSERT_TRUE(bridge.TryEnqueue(second));

    std::vector<ChatResponse> responses;
    ASSERT_TRUE(WaitFor([&]()
    {
        std::vector<ChatResponse> drained = bridge.DrainResponses();
        responses.insert(responses.end(), drained.begin(), drained.end());
        return !responses.empty();
    }, 5000));

    bridge.Stop();

    ASSERT_EQ(responses.size(), 1u);
    EXPECT_EQ(responses[0].requestId, 22u);
    EXPECT_EQ(responses[0].message, "back again");
}

// Typed social protocol ----------------------------------------------------------------------------

namespace
{
    std::string const SOCIAL_TOKEN(40, 'k');

    ClaudeChat::Actor SocialActor(uint64 guid, std::string name, bool human)
    {
        ClaudeChat::Actor actor;
        actor.guidCounter = guid;
        actor.name = std::move(name);
        actor.human = human;
        return actor;
    }

    std::string SocialPayload(std::string kind = "social", uint64 requestToken = 77, uint64 botGuid = 500,
                              std::string message = "Aye, that pack hits hard.", uint64 regenerate = 0,
                              uint64 schema = ClaudeChat::SCHEMA_VERSION, uint64 emoteId = 0,
                              uint64 channel = 2)
    {
        std::string out = "{\"schema_version\":" + std::to_string(schema);
        out += ",\"token\":\"" + SOCIAL_TOKEN + "\"";
        out += ",\"kind\":\"" + kind + "\"";
        out += ",\"social_request_token\":" + std::to_string(requestToken);
        out += ",\"bot_guid\":" + std::to_string(botGuid);
        out += ",\"speak_on_channel\":" + std::to_string(channel);
        out += ",\"message\":\"" + message + "\"";
        out += ",\"emote_id\":" + std::to_string(emoteId);
        out += ",\"regenerate\":" + std::to_string(regenerate);
        out += "}";
        return out;
    }
}

TEST(ClaudeChatSocialProtocolTest, BotAndHumanSpeakersUseTheSameActorShape)
{
    /*
     * Definition of Done 2. Two shapes would let a prompt builder treat the two differently by
     * accident, and the contract is explicit that a human's priority comes from being actively
     * engaged rather than from being human, which only holds if both are described identically.
     */
    ClaudeChat::SocialRequest request;
    request.socialRequestToken = 77;
    request.bot = SocialActor(500, "Grimbold", false);
    request.subject = SocialActor(900, "Deszy", true);
    request.speakOnChannel = 2;
    request.threadPublicId = "thr_00000000000000000000000000000001";
    request.context = "party pull";

    std::optional<std::string> const serialized = ClaudeChat::SerializeSocialRequest(request, SOCIAL_TOKEN);
    ASSERT_TRUE(serialized.has_value());
    std::string const& payload = *serialized;

    // The same field suffixes for both, differing only in the flag.
    EXPECT_NE(payload.find("\"bot_guid\":500"), std::string::npos);
    EXPECT_NE(payload.find("\"bot_name\":\"Grimbold\""), std::string::npos);
    EXPECT_NE(payload.find("\"bot_human\":0"), std::string::npos);
    EXPECT_NE(payload.find("\"subject_guid\":900"), std::string::npos);
    EXPECT_NE(payload.find("\"subject_name\":\"Deszy\""), std::string::npos);
    EXPECT_NE(payload.find("\"subject_human\":1"), std::string::npos);
    EXPECT_NE(payload.find("\"kind\":\"social\""), std::string::npos);
}

TEST(ClaudeChatSocialProtocolTest, AnUnusableActorIsRefusedBeforeItIsSerialized)
{
    EXPECT_TRUE(ClaudeChat::ActorIsUsable(SocialActor(500, "Grimbold", false)));
    EXPECT_FALSE(ClaudeChat::ActorIsUsable(SocialActor(0, "Grimbold", false)));
    EXPECT_FALSE(ClaudeChat::ActorIsUsable(SocialActor(500, "", false)));
    EXPECT_FALSE(ClaudeChat::ActorIsUsable(SocialActor(500, std::string(ClaudeChat::MAX_ACTOR_NAME_BYTES + 1, 'a'),
                                                       false)));
    EXPECT_FALSE(ClaudeChat::ActorIsUsable(SocialActor(500, "Grim\nbold", false)));
}

TEST(ClaudeChatSocialProtocolTest, AWellFormedSocialAnswerIsAccepted)
{
    std::optional<ClaudeChat::SocialResponse> const parsed =
        ClaudeChat::ParseSocialResponsePayload(SocialPayload(), SOCIAL_TOKEN, 77, 500);

    ASSERT_TRUE(parsed.has_value());
    EXPECT_EQ(parsed->socialRequestToken, 77u);
    EXPECT_EQ(parsed->botGuidCounter, 500u);
    EXPECT_EQ(parsed->speakOnChannel, 2u);
    EXPECT_EQ(parsed->message, "Aye, that pack hits hard.");
    EXPECT_FALSE(parsed->regenerate);
}

TEST(ClaudeChatSocialProtocolTest, ACareerDecisionCannotArriveAsASocialLine)
{
    // Definition of Done 3 and 4. Career and social answers travel the same socket, so telling them
    // apart by shape rather than by declaration is how a crafting choice ends up spoken in a zone.
    EXPECT_FALSE(
        ClaudeChat::ParseSocialResponsePayload(SocialPayload("career"), SOCIAL_TOKEN, 77, 500).has_value());
    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(SocialPayload("chat"), SOCIAL_TOKEN, 77, 500).has_value());

    // And a near miss is not a match either.
    EXPECT_FALSE(
        ClaudeChat::ParseSocialResponsePayload(SocialPayload("Social"), SOCIAL_TOKEN, 77, 500).has_value());
    EXPECT_FALSE(
        ClaudeChat::ParseSocialResponsePayload(SocialPayload("social_draft"), SOCIAL_TOKEN, 77, 500).has_value());
}

TEST(ClaudeChatSocialProtocolTest, AnAnswerToADifferentRequestOrBotIsRefused)
{
    // A perfectly well formed answer is still not this answer.
    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(SocialPayload(), SOCIAL_TOKEN, 78, 500).has_value());
    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(SocialPayload(), SOCIAL_TOKEN, 77, 501).has_value());
}

TEST(ClaudeChatSocialProtocolTest, AMismatchedSchemaOrTokenIsRefused)
{
    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(SocialPayload("social", 77, 500, "hi", 0, 2), SOCIAL_TOKEN,
                                                        77, 500)
                     .has_value());
    EXPECT_FALSE(
        ClaudeChat::ParseSocialResponsePayload(SocialPayload(), std::string(40, 'z'), 77, 500).has_value());
}

TEST(ClaudeChatSocialProtocolTest, UnknownFieldsAndOversizeMessagesAreRefused)
{
    // Definition of Done 4.
    std::string extra = SocialPayload();
    extra.insert(extra.size() - 1, ",\"extra\":1");
    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(extra, SOCIAL_TOKEN, 77, 500).has_value());

    std::string const oversize(ClaudeChat::MAX_RESPONSE_MESSAGE_BYTES + 1, 'a');
    EXPECT_FALSE(
        ClaudeChat::ParseSocialResponsePayload(SocialPayload("social", 77, 500, oversize), SOCIAL_TOKEN, 77, 500)
            .has_value());

    // An empty line is not a deliverable answer.
    EXPECT_FALSE(
        ClaudeChat::ParseSocialResponsePayload(SocialPayload("social", 77, 500, ""), SOCIAL_TOKEN, 77, 500)
            .has_value());
}

TEST(ClaudeChatSocialProtocolTest, ARegenerationMayCarryNoMessage)
{
    // The sidecar saying its own output was unusable. It has nothing to deliver, so it is not held
    // to the deliverable line rule, and the coordinator decides whether to ask again.
    std::optional<ClaudeChat::SocialResponse> const parsed =
        ClaudeChat::ParseSocialResponsePayload(SocialPayload("social", 77, 500, "", 1), SOCIAL_TOKEN, 77, 500);

    ASSERT_TRUE(parsed.has_value());
    EXPECT_TRUE(parsed->regenerate);
    EXPECT_TRUE(parsed->message.empty());

    // At most one, so a sidecar returning malformed output forever cannot be retried indefinitely.
    EXPECT_EQ(ClaudeChat::MAX_REGENERATIONS_PER_REQUEST, 1u);
}

TEST(ClaudeChatSocialProtocolTest, ARegenerationFlagOutsideItsRangeIsRefused)
{
    EXPECT_FALSE(
        ClaudeChat::ParseSocialResponsePayload(SocialPayload("social", 77, 500, "hi", 2), SOCIAL_TOKEN, 77, 500)
            .has_value());
}

TEST(ClaudeChatSocialProtocolTest, TheResponseKindEnumFailsClosed)
{
    // Neither -Wswitch nor -Werror is on, so a value cast in from a payload reaches a consumer
    // unchallenged unless the predicate refuses it.
    EXPECT_TRUE(ClaudeChat::ResponseKindIsValid(ClaudeChat::ResponseKind::Chat));
    EXPECT_TRUE(ClaudeChat::ResponseKindIsValid(ClaudeChat::ResponseKind::Career));
    EXPECT_TRUE(ClaudeChat::ResponseKindIsValid(ClaudeChat::ResponseKind::Social));
    EXPECT_FALSE(ClaudeChat::ResponseKindIsValid(static_cast<ClaudeChat::ResponseKind>(77)));
    EXPECT_STREQ(ClaudeChat::ResponseKindName(static_cast<ClaudeChat::ResponseKind>(77)), "unknown");

    EXPECT_FALSE(ClaudeChat::ResponseKindFromName("").has_value());
    EXPECT_FALSE(ClaudeChat::ResponseKindFromName("unknown").has_value());
}

TEST(ClaudeChatSocialProtocolTest, TheProtocolVersionMovedSoAnOlderSidecarIsRefused)
{
    // Fail closed on a mismatched protocol: a sidecar speaking the previous version is rejected
    // outright rather than partially understood.
    EXPECT_EQ(ClaudeChat::SCHEMA_VERSION, 3u);
}

TEST(ClaudeChatSocialProtocolTest, ALegacyChatAnswerIsNotASocialAnswerAndViceVersa)
{
    /*
     * The two parsers are mutually exclusive by field count as well as by kind, so neither can be
     * fed the other's payload even if a caller mixed them up.
     */
    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(
                     "{\"schema_version\":3,\"token\":\"" + SOCIAL_TOKEN + "\",\"request_id\":1,\"message\":\"hi\"}",
                     SOCIAL_TOKEN, 77, 500)
                     .has_value());
    EXPECT_FALSE(ClaudeChat::ParseResponsePayload(SocialPayload(), SOCIAL_TOKEN).has_value());
}

TEST(ClaudeChatSocialProtocolTest, EveryLegacyConversationalHookYieldsToTheCoordinator)
{
    /*
     * Definition of Done 1. The direct whisper, explicit party, and milestone captures each select a
     * responder and send chat on their own, so while the social feature is on they must yield rather
     * than produce a second, unrelated answer to the same message.
     *
     * Kept rather than deleted: with the gate off these are still the only thing that answers a
     * whisper, which is the compatibility requirement.
     */
    EXPECT_FALSE(ClaudeChat::LegacyConversationalHookAllowed(true));
    EXPECT_TRUE(ClaudeChat::LegacyConversationalHookAllowed(false));

    // The ambient World limiter already yielded, and still does.
    EXPECT_FALSE(ClaudeChat::LegacyAmbientWorldAllowed(true, true));
    EXPECT_TRUE(ClaudeChat::LegacyAmbientWorldAllowed(true, false));
    EXPECT_FALSE(ClaudeChat::LegacyAmbientWorldAllowed(false, false));
}

TEST(ClaudeChatSocialProtocolTest, AnExchangeDeliversRegeneratesOnceThenAbandons)
{
    /*
     * Key Decision 5. One regeneration covers a transient glitch; a sidecar that keeps reporting its
     * own output unusable must not be retried forever on one request, and the coordinator rather
     * than this class decides whether a second REQUEST is worth making.
     */
    ClaudeChat::SocialExchange exchange(77, 500);
    ClaudeChat::SocialResponse out;

    EXPECT_EQ(exchange.Classify(SocialPayload("social", 77, 500, "", 1), SOCIAL_TOKEN, out),
              ClaudeChat::SocialExchangeOutcome::Regenerate);
    EXPECT_EQ(exchange.Regenerations(), 1u);

    // The budget is spent, so a second request to try again is abandoned rather than honoured.
    EXPECT_EQ(exchange.Classify(SocialPayload("social", 77, 500, "", 1), SOCIAL_TOKEN, out),
              ClaudeChat::SocialExchangeOutcome::Abandon);
    EXPECT_EQ(exchange.Regenerations(), 1u);

    // A usable line still delivers afterwards.
    EXPECT_EQ(exchange.Classify(SocialPayload(), SOCIAL_TOKEN, out), ClaudeChat::SocialExchangeOutcome::Deliver);
    EXPECT_EQ(out.message, "Aye, that pack hits hard.");
}

TEST(ClaudeChatSocialProtocolTest, AnExchangeFailsClosedOnEveryBadPayload)
{
    // A missing sidecar, a protocol mismatch, and an invalid response all arrive as "this did not
    // parse", and every one of them abandons rather than delivering.
    ClaudeChat::SocialExchange exchange(77, 500);
    ClaudeChat::SocialResponse out;

    for (std::string const& bad : {std::string(""), std::string("not json"), SocialPayload("career"),
                                   SocialPayload("social", 78), SocialPayload("social", 77, 501)})
    {
        EXPECT_EQ(exchange.Classify(bad, SOCIAL_TOKEN, out), ClaudeChat::SocialExchangeOutcome::Abandon);
    }

    // None of those spent the regeneration budget, so a real glitch afterwards still gets its retry.
    EXPECT_EQ(exchange.Regenerations(), 0u);
    EXPECT_EQ(exchange.Classify(SocialPayload("social", 77, 500, "", 1), SOCIAL_TOKEN, out),
              ClaudeChat::SocialExchangeOutcome::Regenerate);
}

TEST(ClaudeChatSocialProtocolTest, TheExchangeOutcomeEnumFailsClosed)
{
    EXPECT_TRUE(ClaudeChat::SocialExchangeOutcomeIsValid(ClaudeChat::SocialExchangeOutcome::Deliver));
    EXPECT_TRUE(ClaudeChat::SocialExchangeOutcomeIsValid(ClaudeChat::SocialExchangeOutcome::Regenerate));
    EXPECT_TRUE(ClaudeChat::SocialExchangeOutcomeIsValid(ClaudeChat::SocialExchangeOutcome::Abandon));
    EXPECT_FALSE(ClaudeChat::SocialExchangeOutcomeIsValid(static_cast<ClaudeChat::SocialExchangeOutcome>(88)));
}

TEST(ClaudeChatSocialProtocolTest, ALegacyResponseInFlightIsDroppedIfTheGateTakesOverMeanwhile)
{
    /*
     * The gate is rechecked at DELIVERY, not only at capture. A request enqueued while the social
     * feature was off can come back after it turned on, and delivering it then sends chat the
     * coordinator now owns, chosen by a rule that no longer applies.
     *
     * The predicate is what the delivery path consults, so this pins the rule the path depends on.
     */
    EXPECT_FALSE(ClaudeChat::LegacyConversationalHookAllowed(true));
    EXPECT_TRUE(ClaudeChat::LegacyConversationalHookAllowed(false));
}

TEST(ClaudeChatSocialProtocolTest, ASocialRequestIsRefusedBeforeAnOversizeFrameIsBuilt)
{
    /*
     * The sidecar enforces these bounds too, but a bound checked only on the far side means an
     * oversize frame is built, sent, and rejected, and the caller learns nothing about which request
     * was at fault. std::string::size() is a byte count in C++, so these are the same bounds rather
     * than a looser character version.
     */
    ClaudeChat::SocialRequest good;
    good.socialRequestToken = 77;
    good.bot = SocialActor(500, "Grimbold", false);
    good.subject = SocialActor(900, "Deszy", true);
    good.threadPublicId = "thr_00000000000000000000000000000001";
    good.context = "party pull";
    ASSERT_TRUE(ClaudeChat::SerializeSocialRequest(good, SOCIAL_TOKEN).has_value());

    // An absent subject is allowed: not every social opportunity is about somebody.
    ClaudeChat::SocialRequest noSubject = good;
    noSubject.subject = ClaudeChat::Actor{};
    EXPECT_TRUE(ClaudeChat::SerializeSocialRequest(noSubject, SOCIAL_TOKEN).has_value());

    for (auto const& [name, mutate] :
         std::initializer_list<std::pair<char const*, void (*)(ClaudeChat::SocialRequest&)>>{
             {"no token", [](ClaudeChat::SocialRequest& r) { r.socialRequestToken = 0; }},
             {"unusable bot", [](ClaudeChat::SocialRequest& r) { r.bot.guidCounter = 0; }},
             {"unusable subject", [](ClaudeChat::SocialRequest& r) { r.subject.name = std::string(200, 'a'); }},
             {"empty thread", [](ClaudeChat::SocialRequest& r) { r.threadPublicId.clear(); }},
             {"long thread",
              [](ClaudeChat::SocialRequest& r) { r.threadPublicId = std::string(ClaudeChat::MAX_THREAD_ID_BYTES + 1, 'a'); }},
             {"long context",
              [](ClaudeChat::SocialRequest& r) { r.context = std::string(ClaudeChat::MAX_SOCIAL_CONTEXT_BYTES + 1, 'a'); }}})
    {
        ClaudeChat::SocialRequest bad = good;
        mutate(bad);
        EXPECT_FALSE(ClaudeChat::SerializeSocialRequest(bad, SOCIAL_TOKEN).has_value()) << name;
    }
}

TEST(ClaudeChatSocialProtocolTest, TheBridgeTokenIsBoundedLikeEveryOtherProtocolString)
{
    // It had a floor for entropy and a frame ceiling that bounded it only incidentally. The rule
    // this protocol claims is that no string is bounded incidentally.
    EXPECT_FALSE(ClaudeChat::BridgeTokenIsUsable(std::string(ClaudeChat::MIN_BRIDGE_TOKEN_BYTES - 1, 'k')));
    EXPECT_TRUE(ClaudeChat::BridgeTokenIsUsable(std::string(ClaudeChat::MIN_BRIDGE_TOKEN_BYTES, 'k')));
    EXPECT_TRUE(ClaudeChat::BridgeTokenIsUsable(std::string(ClaudeChat::MAX_BRIDGE_TOKEN_BYTES, 'k')));
    EXPECT_FALSE(ClaudeChat::BridgeTokenIsUsable(std::string(ClaudeChat::MAX_BRIDGE_TOKEN_BYTES + 1, 'k')));
}

TEST(ClaudeChatSocialProtocolTest, ASubjectIsEitherFullyPresentOrFullyAbsent)
{
    /*
     * A zero guid with a name still attached is an orphan: nothing can resolve it, but it still
     * travels, and a prompt builder reading the name would describe a participant who is not there.
     */
    ClaudeChat::SocialRequest request;
    request.socialRequestToken = 77;
    request.bot = SocialActor(500, "Grimbold", false);
    request.threadPublicId = "thr_00000000000000000000000000000001";

    // Fully absent is legal.
    EXPECT_TRUE(ClaudeChat::SerializeSocialRequest(request, SOCIAL_TOKEN).has_value());

    // Fully present is legal.
    request.subject = SocialActor(900, "Deszy", true);
    EXPECT_TRUE(ClaudeChat::SerializeSocialRequest(request, SOCIAL_TOKEN).has_value());

    // Half described is not.
    request.subject = SocialActor(0, "Deszy", false);
    EXPECT_FALSE(ClaudeChat::SerializeSocialRequest(request, SOCIAL_TOKEN).has_value());

    request.subject = ClaudeChat::Actor{};
    request.subject.human = true;
    EXPECT_FALSE(ClaudeChat::SerializeSocialRequest(request, SOCIAL_TOKEN).has_value());

    EXPECT_TRUE(ClaudeChat::ActorIsAbsent(ClaudeChat::Actor{}));
    EXPECT_FALSE(ClaudeChat::ActorIsAbsent(SocialActor(0, "Deszy", false)));
}

TEST(ClaudeChatSocialProtocolTest, AnUnusableBridgeTokenRefusesTheRequest)
{
    ClaudeChat::SocialRequest request;
    request.socialRequestToken = 77;
    request.bot = SocialActor(500, "Grimbold", false);
    request.threadPublicId = "thr_00000000000000000000000000000001";

    EXPECT_FALSE(ClaudeChat::SerializeSocialRequest(request, "short").has_value());
    EXPECT_FALSE(
        ClaudeChat::SerializeSocialRequest(request, std::string(ClaudeChat::MAX_BRIDGE_TOKEN_BYTES + 1, 'k'))
            .has_value());
}

TEST(ClaudeChatSocialProtocolTest, EveryLegacyStringIsBoundedOnTheCppSideToo)
{
    /*
     * The sidecar bounded these and C++ did not, so the far side was the only thing standing between
     * a bad request and an oversize frame. That asymmetry is the same shape as the four before it.
     */
    ClaudeChat::ChatRequest request;
    request.requestId = 1;
    request.channel = ClaudeChat::ChatChannel::Whisper;
    request.botGuidCounter = 500;
    request.speakerGuidCounter = 900;
    request.botName = "Grimbold";
    request.speakerName = "Deszy";
    request.message = "hello";
    ASSERT_TRUE(ClaudeChat::SerializeRequest(request, SOCIAL_TOKEN).has_value());

    // An unusable token is refused before anything is signed with it.
    EXPECT_FALSE(ClaudeChat::SerializeRequest(request, "short").has_value());

    ClaudeChat::ChatRequest longBot = request;
    longBot.botName = std::string(ClaudeChat::MAX_ACTOR_NAME_BYTES + 1, 'a');
    EXPECT_FALSE(ClaudeChat::SerializeRequest(longBot, SOCIAL_TOKEN).has_value());

    ClaudeChat::ChatRequest longSpeaker = request;
    longSpeaker.speakerName = std::string(ClaudeChat::MAX_ACTOR_NAME_BYTES + 1, 'a');
    EXPECT_FALSE(ClaudeChat::SerializeRequest(longSpeaker, SOCIAL_TOKEN).has_value());

    ClaudeChat::ChatRequest longMessage = request;
    longMessage.message = std::string(ClaudeChat::MAX_REQUEST_MESSAGE_BYTES + 1, 'a');
    EXPECT_FALSE(ClaudeChat::SerializeRequest(longMessage, SOCIAL_TOKEN).has_value());

    // A career payload is a bounded nested document, not one remark, so it keeps its own budget.
    ClaudeChat::ChatRequest career = longMessage;
    career.channel = ClaudeChat::ChatChannel::Career;
    EXPECT_TRUE(ClaudeChat::SerializeRequest(career, SOCIAL_TOKEN).has_value());
}

TEST(ClaudeChatSocialProtocolTest, ACareerTokenIsBoundedWhenParsedBackToo)
{
    // The prefix check proved the shape and nothing proved the size.
    std::string const overlong = "career-" + std::string(ClaudeChat::MAX_CAREER_TOKEN_BYTES, 'a');
    ASSERT_GT(overlong.size(), ClaudeChat::MAX_CAREER_TOKEN_BYTES);

    EXPECT_FALSE(ClaudeChat::ParseCareerDecision(
                     "{\"candidate_token\":\"" + overlong + "\",\"spending_style\":\"minimal\"}")
                     .has_value());
}

TEST(ClaudeChatSocialProtocolTest, ResponseParsersBoundTheTokenExplicitly)
{
    /*
     * Equality against a validated token and the frame ceiling bounded this only as a side effect.
     * The rule this protocol claims is that no string is bounded as a side effect, and a response
     * parser handed an unusable expected token should refuse rather than compare against it.
     */
    std::string const tooLong(ClaudeChat::MAX_BRIDGE_TOKEN_BYTES + 1, 'k');

    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(SocialPayload(), tooLong, 77, 500).has_value());
    EXPECT_FALSE(ClaudeChat::ParseResponsePayload(
                     "{\"schema_version\":3,\"token\":\"" + tooLong + "\",\"request_id\":1,\"message\":\"hi\"}",
                     tooLong)
                     .has_value());

    std::string const tooShort(ClaudeChat::MIN_BRIDGE_TOKEN_BYTES - 1, 'k');
    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(SocialPayload(), tooShort, 77, 500).has_value());
}

// Typed social transport ---------------------------------------------------------------------------

namespace
{
    ClaudeChat::SocialRequest MakeSocialRequest(uint64 requestToken = 77, uint64 botGuid = 500)
    {
        ClaudeChat::SocialRequest request;
        request.socialRequestToken = requestToken;
        request.bot = SocialActor(botGuid, "Grimbold", false);
        request.subject = SocialActor(900, "Deszy", true);
        request.speakOnChannel = static_cast<uint8>(PlayerbotSocialChannel::Party);
        request.threadPublicId = "thr_00000000000000000000000000000001";
        request.context = "party pull";
        return request;
    }

    BridgeConfig MakeSocialBridgeConfig(uint16_t port, uint32 queueCapacity = 8)
    {
        BridgeConfig config = MakeBridgeConfig(port, queueCapacity);
        config.token = SOCIAL_TOKEN;
        return config;
    }

    /*
     * The deadline the deterministic timing tests run at, which is the largest one the transport
     * will accept.
     *
     * Those tests hand every instant in, so the deadline costs no wall clock at all and only sets
     * the scale of their synthetic margins. It is the maximum on purpose. One real clock read
     * remains on the path they exercise: `TryEnqueueSocial` refuses a request that has ALREADY
     * expired, a fail fast that reads the steady clock itself and so cannot be handed a synthetic
     * one without pushing this seam down into the bridge. At this scale that check has tens of
     * seconds of slack, so triggering it would take a scheduler pause far longer than the timeouts
     * every other test in this file already depends on.
     */
    int64 constexpr DETERMINISTIC_DEADLINE_MS = static_cast<int64>(PLAYERBOT_SOCIAL_PROVIDER_TIMEOUT_SECONDS) * 1000;

    // Collects one drain's worth of transport outcomes, polling until something arrives.
    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> DrainWithin(
        ClaudeChat::ClaudeSocialTransport& transport, int64 timeoutMs)
    {
        std::vector<ClaudeChat::ClaudeSocialTransport::Completed> drained;
        WaitFor([&]() {
            std::vector<ClaudeChat::ClaudeSocialTransport::Completed> batch = transport.Drain();
            drained.insert(drained.end(), batch.begin(), batch.end());
            return !drained.empty();
        }, timeoutMs);
        return drained;
    }
}

TEST(ClaudeChatSocialTransportTest, ASubmittedRequestReachesTheSidecarAsASocialFrame)
{
    std::atomic<bool> sawSocialFrame{false};
    FakeSidecarServer server([&](std::string const& payload) -> std::optional<std::string> {
        sawSocialFrame = payload.find("\"kind\":\"social\"") != std::string::npos &&
                         payload.find("\"social_request_token\":77") != std::string::npos &&
                         payload.find("\"thread_id\":\"thr_00000000000000000000000000000001\"") !=
                             std::string::npos;
        return SocialPayload();
    });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 5000);
    ASSERT_TRUE(transport.Submit(MakeSocialRequest()));
    EXPECT_EQ(transport.OutstandingCount(), 1u);

    EXPECT_TRUE(WaitFor([&]() { return sawSocialFrame.load(); }, 5000));
    bridge.Stop();
}

TEST(ClaudeChatSocialTransportTest, AUsableLineIsDrainedAsAMessageForTheCoordinator)
{
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return SocialPayload(); });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 5000);
    ASSERT_TRUE(transport.Submit(MakeSocialRequest()));

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const drained = DrainWithin(transport, 5000);
    bridge.Stop();

    ASSERT_EQ(drained.size(), 1u);
    EXPECT_EQ(drained[0].outcome, ClaudeChat::SocialExchangeOutcome::Deliver);
    EXPECT_EQ(drained[0].socialRequestToken, 77u);
    EXPECT_EQ(drained[0].result.requestToken, 77u);
    EXPECT_EQ(drained[0].result.kind, PlayerbotSocialOutputKind::Message);
    EXPECT_EQ(drained[0].result.text, "Aye, that pack hits hard.");
    EXPECT_EQ(drained[0].result.channel, PlayerbotSocialChannel::Party);

    // A delivered exchange is consumed. A result is delivered once or not at all.
    EXPECT_EQ(transport.OutstandingCount(), 0u);
}

TEST(ClaudeChatSocialTransportTest, ScheduleThreeCannotExpressSilenceSoAnEmptyLineIsAbandoned)
{
    /*
     * The coordinator names Silence a legitimate answer, but schema 3's social response has no way
     * to SAY it: a payload that is not a regeneration must carry one clean line, so an empty message
     * is an invalid response rather than a quiet bot.
     *
     * Abandoned rather than delivered as Silence. Reading an empty message as a deliberate choice
     * would give the same meaning to "this bot decided not to speak" and "the sidecar returned
     * nothing", and those are not the same event. Task 10 owns the response models and is where a
     * silence variant belongs; until then this provider produces a line or nothing at all, and the
     * coordinator's Silence and Emote kinds stay unreachable from it.
     */
    FakeSidecarServer server(
        [](std::string const&) -> std::optional<std::string> { return SocialPayload("social", 77, 500, ""); });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 5000);
    ASSERT_TRUE(transport.Submit(MakeSocialRequest()));

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const drained = DrainWithin(transport, 5000);
    bridge.Stop();

    ASSERT_EQ(drained.size(), 1u);
    EXPECT_EQ(drained[0].outcome, ClaudeChat::SocialExchangeOutcome::Abandon);
    EXPECT_EQ(transport.OutstandingCount(), 0u);
}

TEST(ClaudeChatSocialTransportTest, TheSidecarsOwnChannelIsCarriedSoTheCoordinatorCanRefuseASwitch)
{
    /*
     * The channel travels as the sidecar reported it rather than being replaced with the one that was
     * asked for. Substituting the requested channel here would make PlayerbotSocialValidateOutput's
     * ChannelSwitch refusal unreachable, which is how a party remark reaches a zone channel.
     */
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> {
        std::string payload = SocialPayload();
        std::string const asked = "\"speak_on_channel\":2";
        payload.replace(payload.find(asked), asked.size(), "\"speak_on_channel\":0");
        return payload;
    });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 5000);
    ASSERT_TRUE(transport.Submit(MakeSocialRequest()));

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const drained = DrainWithin(transport, 5000);
    bridge.Stop();

    ASSERT_EQ(drained.size(), 1u);
    EXPECT_EQ(drained[0].result.channel, PlayerbotSocialChannel::General);
    EXPECT_EQ(PlayerbotSocialValidateOutput(drained[0].result, PlayerbotSocialChannel::Party),
              PlayerbotSocialDeliveryRejection::ChannelSwitch);
}

TEST(ClaudeChatSocialTransportTest, AnUnreadableChannelIsAbandonedRatherThanCastIntoOne)
{
    // A value outside the enum cannot be handed to the coordinator: this build has neither -Wswitch
    // nor -Werror, so a cast one would reach a consumer unchallenged.
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> {
        std::string payload = SocialPayload();
        std::string const asked = "\"speak_on_channel\":2";
        payload.replace(payload.find(asked), asked.size(), "\"speak_on_channel\":9");
        return payload;
    });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 5000);
    ASSERT_TRUE(transport.Submit(MakeSocialRequest()));

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const drained = DrainWithin(transport, 5000);
    bridge.Stop();

    ASSERT_EQ(drained.size(), 1u);
    EXPECT_EQ(drained[0].outcome, ClaudeChat::SocialExchangeOutcome::Abandon);
    EXPECT_EQ(transport.OutstandingCount(), 0u);
}

TEST(ClaudeChatSocialTransportTest, ARegenerationIsResubmittedOnceAndThenAbandoned)
{
    std::atomic<uint32_t> asks{0};
    FakeSidecarServer server([&](std::string const&) -> std::optional<std::string> {
        ++asks;
        return SocialPayload("social", 77, 500, "", 1);
    });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 5000);
    ASSERT_TRUE(transport.Submit(MakeSocialRequest()));

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> outcomes;
    WaitFor([&]() {
        std::vector<ClaudeChat::ClaudeSocialTransport::Completed> batch = transport.Drain();
        outcomes.insert(outcomes.end(), batch.begin(), batch.end());
        return outcomes.size() >= 2;
    }, 8000);
    bridge.Stop();

    ASSERT_EQ(outcomes.size(), 2u);
    EXPECT_EQ(outcomes[0].outcome, ClaudeChat::SocialExchangeOutcome::Regenerate);
    EXPECT_EQ(outcomes[1].outcome, ClaudeChat::SocialExchangeOutcome::Abandon);

    // Exactly one retry: the original ask plus one regeneration, never a third.
    EXPECT_EQ(asks.load(), 2u);
    EXPECT_EQ(transport.OutstandingCount(), 0u);
}

TEST(ClaudeChatSocialTransportTest, AnAnswerForARequestNobodyIsWaitingOnIsDropped)
{
    // The bridge tags each answer with the request it was sent for, so this is the case where an
    // exchange was cleared while its answer was in flight. It must not resurrect one.
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return SocialPayload(); });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 5000);
    ASSERT_TRUE(transport.Submit(MakeSocialRequest()));
    transport.Clear();
    EXPECT_EQ(transport.OutstandingCount(), 0u);

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> drained;
    WaitFor([&]() {
        std::vector<ClaudeChat::ClaudeSocialTransport::Completed> batch = transport.Drain();
        drained.insert(drained.end(), batch.begin(), batch.end());
        return !drained.empty();
    }, 1500);
    bridge.Stop();

    EXPECT_TRUE(drained.empty());
    EXPECT_EQ(transport.OutstandingCount(), 0u);
}

TEST(ClaudeChatSocialTransportTest, SubmitFailsClosedOnEveryRefusal)
{
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return SocialPayload(); });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 5000);

    // An unusable actor never reaches the wire.
    ClaudeChat::SocialRequest unusable = MakeSocialRequest(78);
    unusable.bot.name.clear();
    EXPECT_FALSE(transport.Submit(unusable));

    // Neither does a request with no token to tie an answer back to.
    EXPECT_FALSE(transport.Submit(MakeSocialRequest(0)));

    // A token already outstanding is refused rather than replacing the exchange that owns it.
    ASSERT_TRUE(transport.Submit(MakeSocialRequest(77)));
    EXPECT_FALSE(transport.Submit(MakeSocialRequest(77)));
    EXPECT_EQ(transport.OutstandingCount(), 1u);

    bridge.Stop();

    // And a stopped bridge accepts nothing, which the coordinator reads as ProviderFailed.
    EXPECT_FALSE(transport.Submit(MakeSocialRequest(79)));
}

TEST(ClaudeChatSocialTransportTest, TheTransportRefusesBeyondItsOutstandingBound)
{
    /*
     * The coordinator bounds pending deliveries per bot and in total, and this holds the same
     * ceiling. Without it a provider that is never drained accumulates one retained request per
     * token for the rest of the uptime.
     */
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return std::nullopt; });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port(), 4096));

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 60000);
    for (std::size_t i = 0; i < ClaudeChat::MAX_OUTSTANDING_SOCIAL_REQUESTS; ++i)
        ASSERT_TRUE(transport.Submit(MakeSocialRequest(i + 1)));

    EXPECT_EQ(transport.OutstandingCount(), ClaudeChat::MAX_OUTSTANDING_SOCIAL_REQUESTS);
    EXPECT_FALSE(transport.Submit(MakeSocialRequest(ClaudeChat::MAX_OUTSTANDING_SOCIAL_REQUESTS + 1)));
}

TEST(ClaudeChatSocialTransportTest, ARequestNothingEverAnswersIsReleasedByItsOwnDeadline)
{
    /*
     * Most ways a request dies are SILENT. A sidecar that accepts the frame and never replies
     * produces no payload at all, so classification never runs and nothing would ever erase the
     * exchange. Without a deadline the map fills with dead entries and the transport then refuses
     * every later request for the rest of the uptime, which is a permanent outage produced by a
     * temporary one.
     */
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return std::nullopt; });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    // A deadline short enough to observe, and far below the coordinator's own 30 second timeout.
    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 200);
    ASSERT_TRUE(transport.Submit(MakeSocialRequest()));
    EXPECT_EQ(transport.OutstandingCount(), 1u);

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const drained = DrainWithin(transport, 5000);
    bridge.Stop();

    ASSERT_EQ(drained.size(), 1u);
    EXPECT_EQ(drained[0].socialRequestToken, 77u);
    EXPECT_EQ(drained[0].outcome, ClaudeChat::SocialExchangeOutcome::Abandon);
    EXPECT_EQ(transport.OutstandingCount(), 0u);
}

TEST(ClaudeChatSocialTransportTest, TheBoundRecoversOnceDeadRequestsExpire)
{
    // The bound is a ceiling on LIVE requests, not a lifetime quota. A transport that filled up and
    // stayed full would be indistinguishable from one that had simply stopped working.
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return std::nullopt; });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port(), 4096));

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 150);
    for (std::size_t i = 0; i < ClaudeChat::MAX_OUTSTANDING_SOCIAL_REQUESTS; ++i)
        ASSERT_TRUE(transport.Submit(MakeSocialRequest(i + 1)));

    EXPECT_FALSE(transport.Submit(MakeSocialRequest(ClaudeChat::MAX_OUTSTANDING_SOCIAL_REQUESTS + 1)));

    EXPECT_TRUE(WaitFor([&]() {
        transport.Drain();
        return transport.OutstandingCount() == 0;
    }, 5000));

    EXPECT_TRUE(transport.Submit(MakeSocialRequest(ClaudeChat::MAX_OUTSTANDING_SOCIAL_REQUESTS + 1)));
}

TEST(ClaudeChatSocialTransportTest, AConfiguredDeadlineCannotOutliveTheCoordinatorsOwnTimeout)
{
    /*
     * `PlayerbotClaude.ResponseDeadlineMs` has only a floor, so an operator can set it to minutes.
     * The coordinator abandons a request it is still waiting on after its own provider timeout, and
     * a transport that kept holding the slot past that point would sit at its bound refusing new
     * work on behalf of requests nobody is waiting for any more.
     */
    int64 constexpr COORDINATOR_CEILING_MS = static_cast<int64>(PLAYERBOT_SOCIAL_PROVIDER_TIMEOUT_SECONDS) * 1000;

    EXPECT_EQ(ClaudeChat::SocialRequestDeadlineMs(600000), COORDINATOR_CEILING_MS);
    EXPECT_LT(ClaudeChat::SocialRequestDeadlineMs(600000), 600000);

    // Exactly at the ceiling is allowed, and anything under it is the operator's to choose.
    EXPECT_EQ(ClaudeChat::SocialRequestDeadlineMs(COORDINATOR_CEILING_MS), COORDINATOR_CEILING_MS);
    EXPECT_EQ(ClaudeChat::SocialRequestDeadlineMs(5000), 5000);
}

TEST(ClaudeChatSocialTransportTest, AnAnswerThatBeatItsDeadlineIsKeptEvenWhenTheDrainIsLate)
{
    /*
     * Whether an answer was in time is a fact about when it ARRIVED, not about when the world thread
     * next happens to look. Here the answer arrives well inside the deadline but nothing resolves it
     * until long after that deadline has passed. Sweeping expired exchanges before reading the queue
     * would throw this perfectly good line away purely because a tick ran late, which is a dropped
     * conversation with no error anywhere to explain it.
     *
     * Both times are handed in rather than slept out, so the margins are exact rather than likely.
     */
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return std::nullopt; });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));

    int64 const submittedAtMs = ClaudeChat::SteadyNowMs();
    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, DETERMINISTIC_DEADLINE_MS);
    ASSERT_TRUE(transport.SubmitAt(MakeSocialRequest(), submittedAtMs));

    // Arrived 20 seconds inside the deadline; resolved a minute after it had passed.
    std::vector<ClaudeChat::SocialRawResponse> const answered{
        ClaudeChat::SocialRawResponse{77, SocialPayload(), submittedAtMs + 10000}};

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const resolved =
        transport.Resolve(answered, submittedAtMs + 90000);

    ASSERT_EQ(resolved.size(), 1u);
    EXPECT_EQ(resolved[0].socialRequestToken, 77u);
    EXPECT_EQ(resolved[0].outcome, ClaudeChat::SocialExchangeOutcome::Deliver);
    EXPECT_EQ(transport.OutstandingCount(), 0u);
}

TEST(ClaudeChatSocialTransportTest, AnAnswerThatMissedItsDeadlineIsAbandonedEvenIfTheSweepHasNotRun)
{
    /*
     * The other half of the same rule. Reading answers before the sweep must not become a way for a
     * late one to slip in ahead of the exchange that would have been swept, so lateness is decided
     * by the arrival stamp rather than by which loop happens to run first.
     */
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return std::nullopt; });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));

    int64 const submittedAtMs = ClaudeChat::SteadyNowMs();
    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, DETERMINISTIC_DEADLINE_MS);
    ASSERT_TRUE(transport.SubmitAt(MakeSocialRequest(), submittedAtMs));

    // Arrived 30 seconds past the deadline, and resolved immediately afterwards.
    std::vector<ClaudeChat::SocialRawResponse> const answered{
        ClaudeChat::SocialRawResponse{77, SocialPayload(), submittedAtMs + 60000}};

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const resolved =
        transport.Resolve(answered, submittedAtMs + 60001);

    ASSERT_EQ(resolved.size(), 1u);
    EXPECT_EQ(resolved[0].socialRequestToken, 77u);
    EXPECT_EQ(resolved[0].outcome, ClaudeChat::SocialExchangeOutcome::Abandon);
    EXPECT_EQ(transport.OutstandingCount(), 0u);
}

TEST(ClaudeChatSocialTransportTest, ARetryIsGivenAFullDeadlineRatherThanTheOriginalsRemainder)
{
    /*
     * A regeneration is a fresh question, so it gets a fresh deadline. Leaving the exchange on the
     * original one would judge the retry's answer against a clock that started before the retry was
     * even sent, and a sidecar that answered promptly would still be recorded as too late.
     *
     * The second answer lands in the only window where the two behaviours differ: after the original
     * deadline, and inside the extended one.
     *
     *   deadline 30000 ms, so the original expires at +30000
     *   the regeneration is resolved at +20000, so the extension runs to +50000
     *   the retry's answer arrives at +40000: past the original, inside the extension
     */
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return std::nullopt; });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    int64 const submittedAtMs = ClaudeChat::SteadyNowMs();
    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, DETERMINISTIC_DEADLINE_MS);
    ASSERT_TRUE(transport.SubmitAt(MakeSocialRequest(), submittedAtMs));

    std::vector<ClaudeChat::SocialRawResponse> const regenerated{
        ClaudeChat::SocialRawResponse{77, SocialPayload("social", 77, 500, "", 1), submittedAtMs + 10000}};

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const first =
        transport.Resolve(regenerated, submittedAtMs + 20000);

    ASSERT_EQ(first.size(), 1u);
    ASSERT_EQ(first[0].outcome, ClaudeChat::SocialExchangeOutcome::Regenerate);
    ASSERT_EQ(transport.OutstandingCount(), 1u);

    std::vector<ClaudeChat::SocialRawResponse> const answered{
        ClaudeChat::SocialRawResponse{77, SocialPayload(), submittedAtMs + 40000}};

    // Without the extension this is an Abandon: the arrival is past the original deadline, and the
    // sweep would have dropped the exchange at that deadline in any case.
    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const second =
        transport.Resolve(answered, submittedAtMs + 45000);
    bridge.Stop();

    ASSERT_EQ(second.size(), 1u);
    EXPECT_EQ(second[0].outcome, ClaudeChat::SocialExchangeOutcome::Deliver);
    EXPECT_EQ(transport.OutstandingCount(), 0u);
}

TEST(ClaudeChatSocialTransportTest, TheWorkerStampsAnAnswerWithWhenItActuallyArrived)
{
    /*
     * `Resolve` judges by the arrival stamp, so a stamp that was never set would make every answer
     * look like it arrived at time zero and therefore always in time. This is the one test that
     * takes the real path through the socket and the worker, so the stamp has to come from the
     * bridge rather than from the test.
     */
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return SocialPayload(); });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));
    bridge.Start();

    int64 const beforeMs = ClaudeChat::SteadyNowMs();

    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, 5000);
    ASSERT_TRUE(transport.Submit(MakeSocialRequest()));

    // Read straight off the bridge, so the stamp under test is the worker's and not the transport's.
    std::vector<ClaudeChat::SocialRawResponse> drained;
    ASSERT_TRUE(WaitFor([&]() {
        std::vector<ClaudeChat::SocialRawResponse> batch = bridge.DrainSocialResponses();
        drained.insert(drained.end(), batch.begin(), batch.end());
        return !drained.empty();
    }, 5000));

    int64 const afterMs = ClaudeChat::SteadyNowMs();
    bridge.Stop();

    ASSERT_EQ(drained.size(), 1u);
    EXPECT_GE(drained[0].receivedAtSteadyMs, beforeMs);
    EXPECT_LE(drained[0].receivedAtSteadyMs, afterMs);
}

TEST(ClaudeChatSocialProtocolTest, AGestureIsCarriedAsAnEmoteRatherThanAsALine)
{
    /*
     * The coordinator's result already had a kind and an emoteId from Task 7, and its rules for
     * them are built and tested. What was missing was the field on this side of the wire, so a
     * gesture the sidecar chose could never arrive at all.
     */
    std::optional<ClaudeChat::SocialResponse> const parsed = ClaudeChat::ParseSocialResponsePayload(
        SocialPayload("social", 77, 500, "", 0, ClaudeChat::SCHEMA_VERSION, 21), SOCIAL_TOKEN, 77, 500);

    ASSERT_TRUE(parsed.has_value());
    EXPECT_EQ(parsed->emoteId, 21u);
    EXPECT_TRUE(parsed->message.empty());
}

TEST(ClaudeChatSocialProtocolTest, AGestureAndALineTogetherAreTwoAnswersToOneQuestion)
{
    // The coordinator drops text attached to a gesture. Refusing the whole frame is stricter and
    // says which answer was at fault, rather than silently keeping half of one.
    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(
                     SocialPayload("social", 77, 500, "Aye.", 0, ClaudeChat::SCHEMA_VERSION, 21),
                     SOCIAL_TOKEN, 77, 500)
                     .has_value());
}

TEST(ClaudeChatSocialProtocolTest, AnAnswerWithNeitherALineNorAGestureIsRefused)
{
    // Schema 3 cannot express silence, so an empty non-regeneration is a malformed answer rather
    // than a decision not to speak.
    EXPECT_FALSE(ClaudeChat::ParseSocialResponsePayload(
                     SocialPayload("social", 77, 500, "", 0, ClaudeChat::SCHEMA_VERSION, 0), SOCIAL_TOKEN,
                     77, 500)
                     .has_value());
}

TEST(ClaudeChatSocialTransportTest, AGestureReachesTheCoordinatorAsAnEmoteResult)
{
    FakeSidecarServer server([](std::string const&) -> std::optional<std::string> { return std::nullopt; });

    ClaudeBridge bridge(MakeSocialBridgeConfig(server.Port()));

    int64 const submittedAtMs = ClaudeChat::SteadyNowMs();
    ClaudeChat::ClaudeSocialTransport transport(bridge, SOCIAL_TOKEN, DETERMINISTIC_DEADLINE_MS);
    ASSERT_TRUE(transport.SubmitAt(MakeSocialRequest(), submittedAtMs));

    std::vector<ClaudeChat::SocialRawResponse> const answered{ClaudeChat::SocialRawResponse{
        77, SocialPayload("social", 77, 500, "", 0, ClaudeChat::SCHEMA_VERSION, 21), submittedAtMs + 10}};

    std::vector<ClaudeChat::ClaudeSocialTransport::Completed> const resolved =
        transport.Resolve(answered, submittedAtMs + 20);

    ASSERT_EQ(resolved.size(), 1u);
    ASSERT_EQ(resolved[0].outcome, ClaudeChat::SocialExchangeOutcome::Deliver);
    EXPECT_EQ(resolved[0].result.kind, PlayerbotSocialOutputKind::Emote);
    EXPECT_EQ(resolved[0].result.emoteId, 21u);
    EXPECT_TRUE(resolved[0].result.text.empty());
}
