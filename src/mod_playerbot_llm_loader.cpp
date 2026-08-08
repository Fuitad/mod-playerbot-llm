/*
 * This file is part of the mod-playerbot-llm module.
 */

#include "PlayerbotPersonality.h"

// The module is compiled only against personality contract version 4. A newer private
// playerbots revision that bumps the version must ship a matching module update; failing
// the build here is the compatibility guarantee.
static_assert(PLAYERBOT_PERSONALITY_API_VERSION == 4,
              "mod-playerbot-llm requires playerbot personality API version 4");

void AddPlayerbotLLMScripts();

// Loader entry point: the modules script loader maps the mod-playerbot-llm directory
// to this function name (dashes become underscores).
void Addmod_playerbot_llmScripts()
{
    AddPlayerbotLLMScripts();
}
