# This file is part of the mod-playerbot-claude module. Included inline by
# modules/CMakeLists.txt during AzerothCore configuration.
#
# The module is compiled only against the private mod-playerbots personality contract.
# Configuration fails fast with a clear message when that module is absent, and the
# loader enforces PLAYERBOT_PERSONALITY_API_VERSION == 1 at compile time.

set(PLAYERBOT_CLAUDE_PERSONALITY_HEADER
  "${CMAKE_SOURCE_DIR}/modules/mod-playerbots/src/Bot/Personality/PlayerbotPersonality.h")

if (NOT EXISTS "${PLAYERBOT_CLAUDE_PERSONALITY_HEADER}")
  message(FATAL_ERROR
    "mod-playerbot-claude requires the private mod-playerbots module with the personality "
    "contract (missing ${PLAYERBOT_CLAUDE_PERSONALITY_HEADER}). Install the compatible "
    "mod-playerbots revision recorded in mod-playerbot-claude/PLAYERBOTS_REVISION, or remove "
    "mod-playerbot-claude from the modules directory.")
endif()

if (BUILD_TESTING)
  set(PLAYERBOT_CLAUDE_TEST_SOURCES
    "${CMAKE_CURRENT_LIST_DIR}/tests/ClaudeChatTest.cpp")

  foreach(TEST_SOURCE ${PLAYERBOT_CLAUDE_TEST_SOURCES})
    if (EXISTS "${TEST_SOURCE}")
      set_property(GLOBAL APPEND PROPERTY ACORE_MODULE_TEST_SOURCES "${TEST_SOURCE}")
    endif()
  endforeach()

  set_property(GLOBAL APPEND PROPERTY ACORE_MODULE_TEST_INCLUDES
    "${CMAKE_CURRENT_LIST_DIR}/src")
endif()
