"""Simulatore testuale: prova la conversazione dell'agente senza fare telefonate.

Usa lo stesso prompt e gli stessi strumenti della chiamata vera, su un database
temporaneo. Tu scrivi le risposte del cliente, Claude risponde come l'agente.

    python -m app.simulate --nome "Marco" --centro napoli-centro --obiettivo "perdere peso"
    python -m app.simulate --chat      # stessa conversazione, nella versione per la chat
"""

import argparse
import json
import os
import tempfile

from . import db
from .leads import LeadIn, create_lead
from .llm import ask_claude
from .voice import prompt
from .voice.tools import execute_tool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--nome", default="Marco")
    parser.add_argument("--telefono", default="+393331234567")
    parser.add_argument("--centro", default=None)
    parser.add_argument("--obiettivo", default=None)
    parser.add_argument("--chat", action="store_true", help="usa la versione del copione per la chat")
    args = parser.parse_args()

    db.connect(os.path.join(tempfile.mkdtemp(), "simulazione.db"))
    lead_id = create_lead(LeadIn(source="simulazione", telefono=args.telefono, nome=args.nome,
                                 centro_id=args.centro, obiettivo=args.obiettivo)).lead_id
    lead = db.get_lead(lead_id)

    canale = "chat" if args.chat else "telefono"
    system = prompt.system_prompt(lead, canale)
    opening = prompt.first_message(lead, canale)
    print(f"\nAgente: {opening}\n(scrivi 'fine' per terminare)\n")
    messages: list = []
    # Al telefono l'apertura è pronunciata dall'agente: la passiamo come contesto nel primo messaggio utente
    # (nella chat è già nel prompt di sistema)
    pending_prefix = "" if args.chat else f"[L'agente ha aperto la chiamata dicendo: \"{opening}\"]\n"

    while True:
        user = input("Cliente: ").strip()
        if user.lower() in {"fine", "exit", "quit"}:
            break
        messages.append({"role": "user", "content": pending_prefix + user})
        pending_prefix = ""

        while True:
            response = ask_claude(system, messages)
            if response.stop_reason == "refusal":
                print("Agente: [richiesta rifiutata dal modello]\n")
                messages.pop()
                break
            messages.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    print(f"\nAgente: {block.text.strip()}\n")
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break
            results = []
            for tu in tool_uses:
                out = execute_tool(lead_id, tu.name, tu.input)
                print(f"   ⚙ {tu.name}({json.dumps(tu.input, ensure_ascii=False)})\n   → {out}")
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
            messages.append({"role": "user", "content": results})

    final = db.get_lead(lead_id)
    print(f"\nStato finale del lead: {final['status']}  ·  riepilogo: {final.get('summary') or '-'}")


if __name__ == "__main__":
    main()
