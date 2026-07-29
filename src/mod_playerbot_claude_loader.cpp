/*
 * This file is part of the mod-playerbot-claude module.
 */

#include "PlayerbotPersonality.h"

// The module is compiled only against personality contract version 1. A newer private
// playerbots revision that bumps the version must ship a matching module update; failing
// the build here is the compatibility guarantee.
static_assert(PLAYERBOT_PERSONALITY_API_VERSION == 1,
              "mod-playerbot-claude requires playerbot personality API version 1");

void AddClaudeChatScripts();

// Loader entry point: the modules script loader maps the mod-playerbot-claude directory
// to this function name (dashes become underscores).
void Addmod_playerbot_claudeScripts()
{
    AddClaudeChatScripts();
}
