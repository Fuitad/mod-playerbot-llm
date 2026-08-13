/*
 * This file is part of the mod-playerbots-llm module.
 */

#include "PlayerbotPersonality.h"

// The module is compiled only against personality contract version 4. A newer private
// playerbots revision that bumps the version must ship a matching module update; failing
// the build here is the compatibility guarantee.
static_assert(PLAYERBOT_PERSONALITY_API_VERSION == 4,
              "mod-playerbots-llm requires playerbot personality API version 4");

void AddPlayerbotLLMScripts();

// Loader entry point: the modules script loader maps the mod-playerbots-llm directory
// to this function name (dashes become underscores).
void Addmod_playerbots_llmScripts()
{
    AddPlayerbotLLMScripts();
}
