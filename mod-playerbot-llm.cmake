# This file is part of the mod-playerbot-llm module. Included inline by
# modules/CMakeLists.txt during AzerothCore configuration.
#
# Configuration fails fast when any public Playerbot dependency is absent.
# The loader enforces PLAYERBOT_PERSONALITY_API_VERSION == 4 at compile time.

set(PLAYERBOT_LLM_PERSONALITY_HEADER
  "${CMAKE_SOURCE_DIR}/modules/mod-playerbot-personality/src/Bot/Personality/PlayerbotPersonality.h")
set(PLAYERBOT_LLM_CAREER_HEADER
  "${CMAKE_SOURCE_DIR}/modules/mod-playerbots-economy/src/Bot/Personality/PlayerbotCareerPlan.h")
set(PLAYERBOT_LLM_SOCIAL_HEADER
  "${CMAKE_SOURCE_DIR}/modules/mod-playerbots-social/src/Bot/Social/PlayerbotSocialProvider.h")

if (NOT EXISTS "${PLAYERBOT_LLM_PERSONALITY_HEADER}")
  message(FATAL_ERROR
    "mod-playerbot-llm requires mod-playerbot-personality (missing ${PLAYERBOT_LLM_PERSONALITY_HEADER}).")
endif()
if (NOT EXISTS "${PLAYERBOT_LLM_CAREER_HEADER}")
  message(FATAL_ERROR
    "mod-playerbot-llm requires mod-playerbots-economy (missing ${PLAYERBOT_LLM_CAREER_HEADER}).")
endif()
if (NOT EXISTS "${PLAYERBOT_LLM_SOCIAL_HEADER}")
  message(FATAL_ERROR
    "mod-playerbot-llm requires mod-playerbots-social (missing ${PLAYERBOT_LLM_SOCIAL_HEADER}).")
endif()

if (BUILD_TESTING)
  set(PLAYERBOT_LLM_TEST_SOURCES
    "${CMAKE_CURRENT_LIST_DIR}/tests/PlayerbotLLMTest.cpp")

  foreach(TEST_SOURCE ${PLAYERBOT_LLM_TEST_SOURCES})
    if (EXISTS "${TEST_SOURCE}")
      set_property(GLOBAL APPEND PROPERTY ACORE_MODULE_TEST_SOURCES "${TEST_SOURCE}")
    endif()
  endforeach()

  set_property(GLOBAL APPEND PROPERTY ACORE_MODULE_TEST_INCLUDES
    "${CMAKE_CURRENT_LIST_DIR}/src")
endif()
