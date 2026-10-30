// OpenCode global plugin for Telegram -> active/bound session -> Telegram.
// The Python bridge is the only getUpdates poller; this plugin consumes its
// private local inbox and sends the completed assistant response back.
import {
  chmodSync,
  existsSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs"
import { homedir } from "node:os"
import { join, resolve } from "node:path"

const HOME = homedir()

function expandHome(value) {
  return value === "~" ? HOME : value.startsWith("~/") ? join(HOME, value.slice(2)) : value
}

function envFilePath() {
  const explicit = process.env.OC_TELEGRAM_ENV_FILE
  if (explicit) return resolve(expandHome(explicit))

  const candidates = [
    join(HOME, "opencode-telegram-bridge", ".env"),
    join(HOME, "opencode_telegram_bridge", ".env"),
  ]
  return candidates.find(existsSync) || candidates[0]
}

function readSettings() {
  const values = Object.create(null)
  try {
    const source = readFileSync(envFilePath(), "utf8")
    for (const line of source.split(/\r?\n/)) {
      const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/)
      if (!match) continue
      let value = match[2]
      if (value.length >= 2 && ((value[0] === '"' && value.at(-1) === '"') || (value[0] === "'" && value.at(-1) === "'"))) {
        value = value.slice(1, -1)
      }
      values[match[1]] = value
    }
  } catch {
    // Missing settings fail closed below.
  }

  const stateDir = resolve(expandHome(
    process.env.OC_TELEGRAM_STATE_DIR || values.OC_TELEGRAM_STATE_DIR || join(HOME, ".opencode_telegram"),
  ))
  const allowedChats = new Set(
    (values.TELEGRAM_ALLOWED_CHAT_IDS || "")
      .replaceAll(";", ",")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
  )
  return {
    token: values.TELEGRAM_BOT_TOKEN || "",
    stateDir,
    inboxPath: join(stateDir, "inbox.jsonl"),
    cursorPath: join(stateDir, "plugin.cursor"),
    statePath: join(stateDir, "state.json"),
    allowedChats,
  }
}

const SETTINGS = readSettings()
const SESSION_ID = /^ses_[A-Za-z0-9]{8,64}$/
const POLL_MS = 2000
const TELEGRAM_TEXT_LIMIT = 4000

function loadCursor() {
  try {
    const value = Number.parseInt(readFileSync(SETTINGS.cursorPath, "utf8").trim(), 10)
    return Number.isSafeInteger(value) && value >= 0 ? value : 0
  } catch {
    return 0
  }
}

function saveCursor(updateId) {
  const temp = `${SETTINGS.cursorPath}.tmp`
  try {
    writeFileSync(temp, String(updateId), { encoding: "utf8", mode: 0o600 })
    chmodSync(temp, 0o600)
    renameSync(temp, SETTINGS.cursorPath)
  } catch {
    console.error("[telegram-inbox] could not persist the inbox cursor")
  }
}

function readBinding(chatId) {
  try {
    const state = JSON.parse(readFileSync(SETTINGS.statePath, "utf8"))
    const sessionId = state?.[String(chatId)]?.session_id
    return typeof sessionId === "string" && SESSION_ID.test(sessionId) ? sessionId : null
  } catch {
    return null
  }
}

async function sendTelegram(chatId, text) {
  if (!SETTINGS.token || !chatId || !text) return false
  const chunks = []
  let remaining = text
  while (remaining.length > TELEGRAM_TEXT_LIMIT) {
    let split = remaining.lastIndexOf("\n", TELEGRAM_TEXT_LIMIT)
    if (split < TELEGRAM_TEXT_LIMIT / 2) split = TELEGRAM_TEXT_LIMIT
    chunks.push(remaining.slice(0, split))
    remaining = remaining.slice(split).replace(/^\n+/, "")
  }
  if (remaining) chunks.push(remaining)

  for (const chunk of chunks) {
    try {
      const response = await fetch(`https://api.telegram.org/bot${SETTINGS.token}/sendMessage`, {
        method: "POST",
        redirect: "error",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: String(chatId), text: chunk }),
      })
      if (!response.ok) {
        console.error("[telegram-inbox] reply send failed", response.status)
        return false
      }
    } catch {
      // Never log the fetch exception: implementations may include the URL,
      // which contains the Telegram bot token.
      console.error("[telegram-inbox] reply send failed (network error)")
      return false
    }
  }
  return true
}

async function resolveSession(client, currentSessionID, chatId) {
  const bound = readBinding(chatId)
  if (bound) return { sessionId: bound, requiresBinding: false }

  // Sharing one unbound session across several Telegram users leaks context.
  // Multiple allowlisted chats must each be explicitly bound with /bind.
  if (SETTINGS.allowedChats.size !== 1 || !SETTINGS.allowedChats.has(String(chatId))) {
    return { sessionId: null, requiresBinding: true }
  }

  if (currentSessionID && SESSION_ID.test(currentSessionID)) {
    return { sessionId: currentSessionID, requiresBinding: false }
  }
  try {
    const response = await client.session.list()
    const sessions = response.data || []
    sessions.sort((left, right) => (right.time?.updated || 0) - (left.time?.updated || 0))
    return { sessionId: sessions[0]?.id || null, requiresBinding: false }
  } catch {
    console.error("[telegram-inbox] OpenCode session lookup failed")
    return { sessionId: null, requiresBinding: false }
  }
}

async function inject(client, sessionId, chatId, text) {
  try {
    const response = await client.session.prompt({
      path: { id: sessionId },
      body: { parts: [{ type: "text", text }] },
    })
    if (response.error) {
      await sendTelegram(chatId, "OpenCode could not complete that turn. Check the session in the OpenCode UI.")
      return true
    }
    const parts = response.data?.parts || []
    const reply = parts
      .filter((part) => part?.type === "text" && part.text)
      .map((part) => part.text)
      .join("\n")
      .trim()
    await sendTelegram(
      chatId,
      reply || "OpenCode completed the turn without a text reply. Check the session in the OpenCode UI.",
    )
    return true
  } catch {
    // Do not log exception details from API or fetch layers.
    console.error("[telegram-inbox] OpenCode prompt failed")
    return false
  }
}

export const TelegramInboxPlugin = async ({ client }) => {
  let currentSessionID = null
  let cursor = loadCursor()
  let busy = false
  const warnedUnboundUpdates = new Set()

  async function poll() {
    if (busy) return
    let lines
    try {
      lines = readFileSync(SETTINGS.inboxPath, "utf8").split("\n").filter((line) => line.trim())
    } catch {
      return
    }

    const pending = []
    for (const line of lines) {
      try {
        const entry = JSON.parse(line)
        if (Number.isSafeInteger(entry.update_id) && entry.update_id > cursor) pending.push(entry)
      } catch {
        // Ignore malformed legacy lines; the producer retains source evidence.
      }
    }
    pending.sort((left, right) => left.update_id - right.update_id)
    if (!pending.length) return

    busy = true
    try {
      for (const entry of pending) {
        const chatId = String(entry.chat_id)
        if (!SETTINGS.allowedChats.has(chatId) || !entry.text) {
          cursor = entry.update_id
          saveCursor(cursor)
          continue
        }

        const resolution = await resolveSession(client, currentSessionID, chatId)
        const sessionId = resolution.sessionId
        if (!sessionId) {
          if (!warnedUnboundUpdates.has(entry.update_id)) {
            warnedUnboundUpdates.add(entry.update_id)
            const guidance = resolution.requiresBinding
              ? "Bind this Telegram chat with /bind <session_id> before sending tasks."
              : "No OpenCode session is available yet; your message will be retried."
            await sendTelegram(chatId, guidance)
          }
          break
        }

        if (!(await inject(client, sessionId, chatId, entry.text))) break
        cursor = entry.update_id
        saveCursor(cursor)
      }
    } finally {
      busy = false
    }
  }

  const timer = setInterval(poll, POLL_MS)
  void poll()

  return {
    "chat.message": async (input) => {
      if (input?.sessionID && SESSION_ID.test(input.sessionID)) currentSessionID = input.sessionID
    },
    dispose: async () => clearInterval(timer),
  }
}
