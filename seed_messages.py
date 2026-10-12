"""Seed the whats2api_bridge local tables with a realistic 24h window.

Run inside the container::

    docker exec odysseus-odysseus-1 python3 /tmp/seed_messages.py

Idempotent: re-running upserts by primary key and refreshes ``ts`` so the
messages always fall inside the analyzer's default 24h lookback.
"""

import json
import time
import uuid

from core.database import SessionLocal
from sqlalchemy import text

NOW = int(time.time())
HOUR = 3600

CONNECTION_ID = "conn-seed-0001"

# (remote_jid, push_name, direction, text, hours_ago, message_type)
MESSAGES = [
    # ── Family group: plumbing emergency ────────────────────────────────
    ("5511998877665@s.whatsapp.net", "Ana Souza", "in",
     "Oi! A tubulação da cozinha vazou de novo, tem água no chão inteiro",
     2.0, "text"),
    ("5511998877665@s.whatsapp.net", "Ana Souza", "in",
     "O encanador que a gente chamou cobrou R$480 só pela visita, tá caro demais",
     1.9, "text"),
    ("5511998877665@s.whatsapp.net", "Ana Souza", "in",
     "Você conhece algum encanador de confiança? Preciso resolver hoje ainda",
     1.8, "text"),
    ("5511998877665@s.whatsapp.net", "Ana Souza", "out",
     "Deixa comigo, vou perguntar pro João e te falo em até 1h",
     1.7, "text"),
    ("5511998877665@s.whatsapp.net", "Ana Souza", "in",
     "Obrigada!! Ah, e o cachorro do vizinho fugiu de novo, se aparecer me avisa",
     1.5, "text"),

    # ── Work group: report deadline + invoice ───────────────────────────
    ("5511911112222@g.us", "Time Comercial", "in",
     "Pessoal, o relatório do Q3 precisa sair até sexta 18h, cliente cobrando",
     5.0, "text"),
    ("5511911112222@g.us", "Time Comercial", "in",
     "A fatura da AWS chegou em R$12.480, 32% acima do mês passado, alguém revisou?",
     4.5, "text"),
    ("5511911112222@g.us", "Time Comercial", "in",
     "@todos precisamos do número final de licenças até amanhã de manhã",
     4.0, "text"),
    ("5511911112222@g.us", "Time Comercial", "out",
     "Licenças: 148 ativas, 12 em trial. Mando a planilha hoje à tarde",
     3.5, "text"),
    ("5511911112222@g.us", "Time Comercial", "in",
     "Perfeito. E alguém consegue olhar o contrato do fornecedor novo? Vence em 10 dias",
     3.0, "text"),
    ("5511911112222@g.us", "Time Comercial", "in",
     "Lembrete: reunião com o cliente reagendada pra quinta 14h",
     0.8, "text"),

    # ── Dentist + dentist appointment ───────────────────────────────────
    ("5511933334444@s.whatsapp.net", "Clínica Sorriso", "in",
     "Olá! Confirmamos sua limpeza para amanhã às 15h. Confirma presença?",
     8.0, "text"),
    ("5511933334444@s.whatsapp.net", "Clínica Sorriso", "in",
     "Se precisar remarcar, avise com 24h de antecedência por favor",
     7.9, "text"),
    ("5511933334444@s.whatsapp.net", "Clínica Sorriso", "out",
     "Confirmado, até amanhã às 15h. Já vou levar a lista de remédios",
     7.0, "text"),
    ("5511933334444@s.whatsapp.net", "Clínica Sorriso", "in",
     "Ótimo! Trazer também o cartão do convênio. Até lá.",
     6.5, "text"),

    # ── Landlord / building ─────────────────────────────────────────────
    ("5511955556666@s.whatsapp.net", "Síndico Paulo", "in",
     "Amanhã das 9h às 12h terá manutenção do elevador, evite usar",
     26.0, "text"),
    ("5511955556666@s.whatsapp.net", "Síndico Paulo", "in",
     "A taxa extra do rateio sai R$210 por apartamento esse mês, por causa do conserto da bomba",
     25.0, "text"),
    ("5511955556666@s.whatsapp.net", "Síndico Paulo", "out",
     "Entendi, vou depositar até dia 10. O boleto já saiu?",
     24.5, "text"),
    ("5511955556666@s.whatsapp.net", "Síndico Paulo", "in",
     "Saiu sim, te mando por e-mail hoje à noite. Abraço",
     24.2, "text"),
]


def main() -> None:
    db = SessionLocal()
    try:
        db.execute(text(
            "INSERT OR REPLACE INTO whats2api_bridge__connections "
            "(id, name, engine, phone, status, owner_id, raw_json, updated_at) "
            "VALUES (:id, :name, :engine, :phone, :status, :owner_id, :raw_json, :updated_at)"
        ), {
            "id": CONNECTION_ID,
            "name": "Seed WhatsApp",
            "engine": "whats2api",
            "phone": "+5511987654321",
            "status": "connected",
            "owner_id": "",
            "raw_json": json.dumps({"seeded": True}),
            "updated_at": NOW,
        })
        for remote_jid, push_name, direction, body, hours_ago, mtype in MESSAGES:
            ts = NOW - int(hours_ago * HOUR)
            db.execute(text(
                "INSERT OR REPLACE INTO whats2api_bridge__messages "
                "(id, connection_id, direction, remote_jid, from_me, chat_jid, "
                " sender_jid, push_name, text, message_type, ts, raw_json) "
                "VALUES (:id, :cid, :dir, :jid, :from_me, :chat, :sender, "
                " :push_name, :text, :mtype, :ts, :raw)"
            ), {
                "id": f"seed-{uuid.uuid5(uuid.NAMESPACE_URL, remote_jid + str(ts) + body).hex[:16]}",
                "cid": CONNECTION_ID,
                "dir": direction,
                "jid": remote_jid,
                "from_me": "1" if direction == "out" else "0",
                "chat": remote_jid,
                "sender": remote_jid,
                "push_name": push_name,
                "text": body,
                "mtype": mtype,
                "ts": ts,
                "raw": json.dumps({"seeded": True}),
            })
        db.commit()
        n = db.execute(text(
            "SELECT COUNT(*) FROM whats2api_bridge__messages"
        )).scalar()
        recent = db.execute(text(
            "SELECT COUNT(*) FROM whats2api_bridge__messages WHERE ts >= :cutoff"
        ), {"cutoff": NOW - 24 * HOUR}).scalar()
        print(f"seeded: {n} messages total, {recent} inside the last 24h")
    finally:
        db.close()


if __name__ == "__main__":
    main()
