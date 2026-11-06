// Voice inquiry panel: "Rover R-0x asks...". The rover speaks its questions (speechSynthesis) and
// listens (SpeechRecognition) so the USER plays the bystander; answers can also be typed, or a
// clearly-labelled simulated bystander can answer. Answers go to POST /api/inquiries/{id}/answers
// and the engine sends them to 0G as a CP-Q checkpoint. Nothing is summarised in the browser.
import { CONFIG } from "../config";
import type { Store } from "../store";
import { el, esc } from "./dom";

type Lang = "hi" | "en";
interface Question { hi: string; en: string; }
interface Inquiry {
  id: string; robot_id: string; status?: string; source?: string; expires_in_s?: number;
  hazard?: { type?: string; description?: string }; questions?: Question[];
}

const LANG_KEY = "hc.inquiry.lang";
const VOICE: Record<Lang, string[]> = { hi: ["hi-IN", "hi"], en: ["en-IN", "en-GB", "en-US", "en"] };

// Fictional canned bystander. Always submitted with source "simulated" and labelled as such.
const SIMULATED: Record<Lang, string[]> = {
  hi: ["आगे दो गाड़ियों की टक्कर हो गई है।", "एक लेन बंद है, इसलिए गाड़ियाँ रुकी हुई हैं।", "पीछे से टक्कर लगी, गाड़ी का बम्पर टूट गया।", "शायद बीस मिनट लगेंगे, पुलिस आ रही है।"],
  en: ["Two cars have collided ahead.", "One lane is blocked, so the traffic has stopped.", "A rear-end crash; a bumper is broken.", "Maybe twenty minutes, the police are coming."],
};

function getLang(): Lang { try { return localStorage.getItem(LANG_KEY) === "en" ? "en" : "hi"; } catch { return "hi"; } }
function setLang(l: Lang): void { try { localStorage.setItem(LANG_KEY, l); } catch { /* private mode */ } }

function pickVoice(lang: Lang): SpeechSynthesisVoice | null {
  const vs = window.speechSynthesis?.getVoices() ?? [];
  for (const tag of VOICE[lang]) {
    const v = vs.find((x) => x.lang.replace("_", "-").toLowerCase().startsWith(tag.toLowerCase()));
    if (v) return v;
  }
  return null;
}

function speak(text: string, lang: Lang): Promise<void> {
  return new Promise((resolve) => {
    const ss = window.speechSynthesis;
    if (!ss) return resolve();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = lang === "hi" ? "hi-IN" : "en-IN";
    const v = pickVoice(lang);
    if (v) u.voice = v;
    u.onend = () => resolve();
    u.onerror = () => resolve();
    ss.speak(u);
  });
}

type SR = { lang: string; interimResults: boolean; maxAlternatives: number; start(): void; stop(): void;
  onresult: ((e: any) => void) | null; onerror: (() => void) | null; onend: (() => void) | null };

function listen(lang: Lang, maxMs = 6000): Promise<string> {
  const Ctor = (window as any).SpeechRecognition ?? (window as any).webkitSpeechRecognition;
  if (!Ctor) return Promise.resolve("");
  return new Promise((resolve) => {
    const r: SR = new Ctor();
    let text = "";
    r.lang = lang === "hi" ? "hi-IN" : "en-IN";
    r.interimResults = false;
    r.maxAlternatives = 1;
    r.onresult = (e) => { text = String(e.results?.[0]?.[0]?.transcript ?? ""); };
    r.onerror = () => resolve(text);
    r.onend = () => resolve(text);
    try { r.start(); } catch { return resolve(""); }
    setTimeout(() => { try { r.stop(); } catch { /* already stopped */ } }, maxMs);
  });
}

export function initInquiry(store: Store): void {
  let host: HTMLElement | null = null;
  let cur: Inquiry | null = null;
  let deadline = 0;
  let timer = 0;
  let aborted = false;
  let lang = getLang();

  const hasVoice = !!window.speechSynthesis;
  const hasMic = !!((window as any).SpeechRecognition ?? (window as any).webkitSpeechRecognition);

  function close(): void {
    aborted = true;
    window.speechSynthesis?.cancel();
    clearInterval(timer);
    host?.remove(); host = null; cur = null;
  }

  async function send(source: "user" | "simulated", answers?: { q: string; a: string; lang: Lang }[]): Promise<void> {
    if (!cur) return;
    const inq = cur;
    const body = answers ?? collect();
    try {
      await fetch(`${CONFIG.apiBase}/api/inquiries/${inq.id}/answers`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answers: body, source }),
      });
    } catch { /* server gone: the inquiry just expires */ }
    close();
  }

  function collect(): { q: string; a: string; lang: Lang }[] {
    const qs = cur?.questions ?? [];
    return qs.map((q, i) => ({
      q: q[lang], a: host?.querySelector<HTMLInputElement>(`input[data-i="${i}"]`)?.value.trim() ?? "", lang,
    }));
  }

  function render(): void {
    if (!cur) return;
    const qs = cur.questions ?? [];
    host?.remove();
    host = el("div", "inq");
    host.innerHTML = `
      <div class="inq-head"><span class="inq-pulse" title="speaker and mic on">🔊🎙</span>
        <b>Rover ${esc(cur.robot_id)} asks bystanders…</b>
        <span class="inq-count"></span><button class="inq-x" aria-label="Skip">✕</button></div>
      <div class="inq-sub">${esc(cur.hazard?.type ?? "hazard")} ahead. Your answers are sent to 0G as untrusted testimony; if you skip, the rover continues on its photo decision only.</div>
      <div class="inq-lang"><button data-l="hi" class="${lang === "hi" ? "on" : ""}">हिन्दी</button><button data-l="en" class="${lang === "en" ? "on" : ""}">English</button></div>
      ${qs.map((q, i) => `<label class="inq-q"><span class="inq-qt" lang="${lang}">${esc(q[lang])}</span>
        <input data-i="${i}" lang="${lang}" type="text" placeholder="${lang === "hi" ? "जवाब लिखें या बोलें" : "type or speak the answer"}"></label>`).join("")}
      <div class="inq-actions">
        <button class="inq-ask" ${hasVoice ? "" : "disabled"} title="${hasVoice ? "" : "speechSynthesis not available in this browser"}">🔊 Ask aloud${hasMic ? " + listen" : ""}</button>
        <button class="inq-send">Send my answers</button>
        <button class="inq-sim" title="Canned fictional answers, flagged source=simulated everywhere">Simulated bystander</button>
      </div>
      <div class="inq-note">${hasMic ? "" : "Speech recognition is not available in this browser: type the answers. "}Simulated answers are fictional and labelled <code>simulated</code> in the card and episode log.</div>`;
    document.body.appendChild(host);
    host.querySelector(".inq-x")!.addEventListener("click", close);
    host.querySelectorAll<HTMLButtonElement>(".inq-lang button").forEach((b) => b.addEventListener("click", () => {
      lang = b.dataset.l as Lang; setLang(lang);
      const keep = collect().map((c) => c.a);
      render();
      host!.querySelectorAll<HTMLInputElement>("input[data-i]").forEach((inp, i) => { inp.value = keep[i] ?? ""; });
    }));
    host.querySelector(".inq-send")!.addEventListener("click", () => void send("user"));
    host.querySelector(".inq-sim")!.addEventListener("click", () =>
      void send("simulated", qs.map((q, i) => ({ q: q[lang], a: SIMULATED[lang][i % SIMULATED[lang].length], lang }))));
    host.querySelector(".inq-ask")!.addEventListener("click", async () => {
      aborted = false;
      host?.classList.add("speaking");
      for (let i = 0; i < qs.length && !aborted; i++) {
        const inp = host?.querySelector<HTMLInputElement>(`input[data-i="${i}"]`);
        await speak(qs[i][lang], lang);
        if (aborted || !inp) break;
        if (hasMic) { inp.placeholder = "…"; const t = await listen(lang); if (t) inp.value = t; }
      }
      host?.classList.remove("speaking");
    });
    const count = host.querySelector<HTMLElement>(".inq-count")!;
    clearInterval(timer);
    timer = window.setInterval(() => {
      const left = Math.max(0, (deadline - Date.now()) / 1000);
      count.textContent = `${Math.ceil(left)} s`;
      if (left <= 2) {   // time is nearly up: send whatever was typed/heard so far
        const got = collect();
        if (got.some((g) => g.a)) void send("user", got); else close();
      }
    }, 250);
  }

  store.on((what, payload) => {
    if (what !== "inquiry") return;
    const d = payload as Inquiry;
    if (d.status === "asking" && d.questions) {
      if (cur) return;   // one panel at a time; the rest simply expire
      cur = d; deadline = Date.now() + (d.expires_in_s ?? 25) * 1000; aborted = false;
      render();
    } else if (cur && d.id === cur.id && d.status && d.status !== "asking") {
      close();
    }
  });
}
