"use client";

/**
 * Ask in plain language, and see how it was read before anything runs.
 *
 * Two steps deliberately. "read it" shows the interpretation; "run it" acts on
 * it. Collapsing them into one press would hide the part most likely to be
 * wrong — and answering a subtly different question than the one asked is how
 * this tool would end up making a claim about a person nobody meant to accuse.
 *
 * Nothing is sent anywhere. The question contains an address, and a forensics
 * tool that transmits which addresses are under investigation has broken its
 * first promise no matter how good the answer is. The parser runs on the local
 * server, against a fixed vocabulary, and refuses rather than guesses.
 */

import { useState } from "react";

import { api, type AskReply } from "@/lib/api";

type Props = {
  chain: string;
  onClose: () => void;
  onRun: (plan: AskReply) => void;
};

export function AskDialog({ chain, onClose, onRun }: Props) {
  const [question, setQuestion] = useState("");
  const [plan, setPlan] = useState<AskReply | null>(null);
  const [error, setError] = useState("");
  const [reading, setReading] = useState(false);

  async function read() {
    if (!question.trim()) return;
    setReading(true);
    setPlan(null);
    setError("");
    try {
      const reply = await api<AskReply>("/ask", {
        q: question.trim(),
        chain,
        // Sent so a relative window becomes a fixed instant here rather than
        // meaning "whenever the server happened to run".
        now: Math.floor(Date.now() / 1000),
      });
      setPlan(reply);
    } catch (err) {
      // The refusal names the vocabulary it does know, so it is the useful part.
      setError((err as Error).message);
    } finally {
      setReading(false);
    }
  }

  return (
    <div className="scrim" onClick={onClose} role="presentation">
      <div className="sheet" onClick={(event) => event.stopPropagation()}>
        <h3>Ask in plain language</h3>
        <p className="note small">
          Nothing is sent anywhere. The question is read on your own machine,
          against a fixed vocabulary, and you are shown what it would run before
          it runs.
        </p>
        <input
          autoFocus
          className="mono"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void read();
            }
          }}
          placeholder="who paid 0x… in the last week"
          spellCheck={false}
        />

        {error ? <p className="cannot">{error}</p> : null}

        {plan ? (
          <div className="plan">
            <p>
              <b>Reading:</b> {plan.reading}
            </p>
            <p className="endpoint">
              {plan.endpoint} {JSON.stringify(plan.params)}
            </p>
            {plan.ignored.length ? (
              <p className="cannot">
                <b>Not honoured</b> {plan.ignored.join("; ")}
              </p>
            ) : null}
            <p className="note small">{plan.caveat}</p>
          </div>
        ) : null}

        <div className="ctl">
          <button onClick={read} disabled={reading}>
            {reading ? "reading…" : "read it"}
          </button>
          <button onClick={() => plan && onRun(plan)} disabled={!plan}>
            run it
          </button>
          <button onClick={onClose}>close</button>
        </div>
      </div>
    </div>
  );
}
