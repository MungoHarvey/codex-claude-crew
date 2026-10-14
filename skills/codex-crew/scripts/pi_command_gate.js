// Pi extension loaded by relay.py (`pi -e`). Pi has no permission rules of its
// own, so this gate blocks every bash command the run's rules do not allow.
// The rules arrive in CODEX_CREW_PI_RULES as {"allow": [...], "deny": [...]},
// using the same patterns as `--allow 'Bash(npm test *)'`. Without them,
// nothing is allowed.

export const BLOCKED = "codex-crew: command not allowed";

// Redirections that cannot write a file; any other `>` is refused.
const HARMLESS_REDIRECTS = /\s(?:2>&1|[12&]?>\s*\/dev\/null)(?=\s|$)/g;
// Command and process substitution or a file write could hide a second command.
const UNSAFE = /`|\$\(|<\(|>\(|>/;

function toRegExp(pattern) {
  const body = pattern.trim().split("*").map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*");
  // Like Claude Code, a trailing " *" also matches the bare command.
  return new RegExp("^" + body.replace(/ \.\*$/, "(?: .*)?") + "$", "s");
}

export function isAllowed(command, rules) {
  const allow = (rules.allow || []).map(toRegExp);
  const deny = (rules.deny || []).map(toRegExp);
  const text = ` ${command} `.replace(HARMLESS_REDIRECTS, " ");
  if (UNSAFE.test(text)) return false;
  const segments = text.split(/\|\||&&|[;|&\n]/).map((part) => part.trim()).filter(Boolean);
  return segments.length > 0 &&
    segments.every((part) => allow.some((rule) => rule.test(part)) && !deny.some((rule) => rule.test(part)));
}

export default function (pi) {
  let rules = { allow: [], deny: [] };
  try {
    rules = JSON.parse(process.env.CODEX_CREW_PI_RULES || "{}");
  } catch {
    // Unreadable rules leave the empty allow list in place: fail closed.
  }
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return undefined;
    const command = String((event.input || {}).command || "");
    if (isAllowed(command, rules)) return undefined;
    return { block: true, reason: `${BLOCKED}: ${command}` };
  });
}
