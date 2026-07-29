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
        request.profile.version = 1;
        request.profile.craftingAffinity = 65;
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
                                     std::string const& token = TEST_TOKEN, uint32 schemaVersion = 1)
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
        "{\"schema_version\":1,"
        "\"token\":\"0123456789abcdef0123456789abcdef\","
        "\"request_id\":7,"
        "\"channel\":\"whisper\","
        "\"bot_guid\":42,"
        "\"speaker_guid\":9001,"
        "\"bot_name\":\"Botname\","
        "\"speaker_name\":\"Speaker\","
        "\"profile_version\":1,"
        "\"crafting_affinity\":65,"
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

    std::string const serialized = SerializeRequest(request, TEST_TOKEN);
    EXPECT_NE(serialized.find("say \\\"hi\\\" \\\\ and\\nrun\\tfast \\u0001 caf\xC3\xA9"), std::string::npos);
}

TEST(ClaudeChatProtocolTest, PartyChannelSerializesAsParty)
{
    ChatRequest request = MakeFixtureRequest();
    request.channel = ChatChannel::Party;
    EXPECT_NE(SerializeRequest(request, TEST_TOKEN).find("\"channel\":\"party\""), std::string::npos);
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
    EXPECT_FALSE(ParseResponsePayload(ValidResponsePayload(7, "hello", TEST_TOKEN, 2), TEST_TOKEN).has_value());
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

    DeliverySnapshot inCombat = good;
    inCombat.botInCombat = true;
    EXPECT_FALSE(ShouldDeliver(ChatChannel::Whisper, inCombat));
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
