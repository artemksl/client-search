"""Pure share-of-voice computation for the free-audit stage."""

from __future__ import annotations

import re
import unicodedata


def aggressive_match_key(s: str) -> str:
    """Fuzzy key for self-identification against `clinic_aliases`.

    Deliberately lossy: it strips the stopwords `the|clinic|clinics|london|ltd|limited`, so
    "The London Clinic" normalizes to the empty string. That is correct here — it is matching
    a clinic against its own aliases, not distinguishing clinics from each other.

    Do NOT merge with `clinic_geo.parsing.clinic_names.normalize_clinic_name`. They look alike
    and are not: swapping this one for the conservative key would change every share-of-voice
    number this module has ever produced.
    """
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&", "and")
    s = re.sub(r"\b(the|clinic|clinics|london|ltd|limited)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def build_share_of_voice(config, basket, extracted_names, matches):
    """Return `(payload, unknown_ids)` matching `free-audit/2_share_of_voice.py` output."""
    clinic_name = (config.get("clinic_name") or "").strip()
    self_keys = {aggressive_match_key(clinic_name)} | {aggressive_match_key(a) for a in config.get("clinic_aliases", [])}
    self_keys.discard("")

    def is_self(name):
        return bool(self_keys) and aggressive_match_key(name) in self_keys

    stratum_by_id = {p["id"]: p.get("stratum", "") for p in basket["prompts"]}
    canon_info = matches["canonical"]

    def canon(raw):
        c = canon_info.get(raw)
        return (c["canonical"], bool(c.get("in_database"))) if c else (raw, False)

    extracted = dict(extracted_names)
    extracted.pop("_failures", None)

    market_counts, in_db = {}, {}
    market_answers = 0
    audited_market_answers = 0
    branded = {"answers": 0, "audited_named_in": 0, "per_prompt": {}, "other_clinics_named": {}}
    unknown_ids = []

    for rid in sorted(extracted):
        base = rid.split("#", 1)[0]
        stratum = stratum_by_id.get(base)
        if stratum is None:
            unknown_ids.append(rid)
            stratum = ""
        names = list(dict.fromkeys(canon(n)[0] for n in extracted[rid]))
        if stratum == "brand_reputation":
            branded["answers"] += 1
            pp = branded["per_prompt"].setdefault(base, {"answers": 0, "audited_named_in": 0})
            pp["answers"] += 1
            if any(is_self(n) for n in names):
                branded["audited_named_in"] += 1
                pp["audited_named_in"] += 1
            for n in names:
                if not is_self(n):
                    branded["other_clinics_named"][n] = branded["other_clinics_named"].get(n, 0) + 1
        else:
            market_answers += 1
            if any(is_self(n) for n in names):
                audited_market_answers += 1
            for n in names:
                market_counts[n] = market_counts.get(n, 0) + 1
                in_db[n] = in_db.get(n, False) or canon(n)[1]

    branded["other_clinics_named"] = dict(sorted(
        branded["other_clinics_named"].items(),
        key=lambda kv: (-kv[1], kv[0]),
    ))
    total = sum(market_counts.values()) or 1

    table, best_rank = [], None
    ranked = sorted(market_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    for rank, (name, count) in enumerate(ranked, start=1):
        is_audited = is_self(name)
        table.append({
            "rank": rank,
            "name": name,
            "in_database": in_db.get(name, False),
            "appearances": count,
            "share": round(count / total, 4),
            "is_audited": is_audited,
        })
        if is_audited and best_rank is None:
            best_rank = rank

    audited = None
    if audited_market_answers:
        audited = {
            "best_rank": best_rank,
            "answers_named": audited_market_answers,
            "appearance_rate": round(audited_market_answers / market_answers, 4) if market_answers else None,
        }

    payload = {
        "clinic_name": clinic_name,
        "audited_clinic": audited,
        "total_appearances": sum(market_counts.values()),
        "market_answers": market_answers,
        "branded_answers": branded["answers"],
        "table": table,
        "branded": branded,
    }
    return payload, unknown_ids
