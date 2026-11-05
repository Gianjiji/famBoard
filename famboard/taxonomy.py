"""Category taxonomy and the mapping from UT1 (Toulouse) blacklist names.

CATEGORIES defines the labels shown in the dashboard. UT1_TO_CATEGORY maps the
directory names in the Universite de Toulouse "Capitole" blacklist tarball to
our category keys. Categories with minor_sensitive=1 are the ones highlighted
as a concern for minors (adult content, gambling, violence, drugs, ...).
"""
from __future__ import annotations

import sqlite3

# key -> (italian label, risk_level, minor_sensitive, color, critical)
# critical=1 -> alerts for ANYONE (not only minors), e.g. distress signals.
CATEGORIES: dict[str, tuple[str, int, int, str, int]] = {
    "social":       ("Social", 0, 0, "#4C6EF5", 0),
    "messaging":    ("Messaggistica", 0, 0, "#3BC9DB", 0),
    "entertainment":("Intrattenimento", 0, 0, "#9775FA", 0),
    "games":        ("Videogiochi", 0, 0, "#F783AC", 0),
    "education":    ("Educazione", 0, 0, "#40C057", 0),
    "news":         ("Notizie", 0, 0, "#748FFC", 0),
    "shopping":     ("Shopping", 0, 0, "#FFA94D", 0),
    "finance":      ("Finanza", 0, 0, "#20C997", 0),
    "search":       ("Ricerca", 0, 0, "#A9E34B", 0),
    "tech":         ("Servizi e tecnologia", 0, 0, "#868E96", 0),
    "ads_tracking": ("Pubblicita e tracker", 0, 0, "#CED4DA", 0),
    # --- sensitive, flagged for minors ---
    "adult":        ("Contenuti per adulti", 2, 1, "#E03131", 0),
    "dating":       ("Incontri", 1, 1, "#F06595", 0),
    "gambling":     ("Gioco d'azzardo", 2, 1, "#D6336C", 0),
    "violence":     ("Violenza", 2, 1, "#C92A2A", 0),
    "drugs":        ("Droghe", 2, 1, "#A61E4D", 0),
    "weapons":      ("Armi", 1, 1, "#E8590C", 0),
    "warez":        ("Pirateria", 1, 1, "#F59F00", 0),
    "malware":      ("Malware e phishing", 2, 0, "#7048E8", 0),
    # --- critical: possible psychological distress, alert for ANYONE ---
    "self_harm":      ("Segnali di disagio / crisi", 2, 1, "#B00020", 1),
    "eating_disorder":("Disturbi alimentari", 2, 1, "#C2185B", 1),
    "extremism":      ("Estremismo e radicalizzazione", 2, 1, "#880808", 1),
}

# UT1 blacklist directory name -> our category key.
UT1_TO_CATEGORY: dict[str, str] = {
    "adult": "adult",
    "pornhub": "adult",
    "dating": "dating",
    "social_networks": "social",
    "chat": "messaging",
    "webmail": "messaging",
    "games": "games",
    "gambling": "gambling",
    "drogue": "drugs",
    "violence": "violence",
    "agressif": "violence",
    "warez": "warez",
    "malware": "malware",
    "phishing": "malware",
    "cryptojacking": "malware",
    "shopping": "shopping",
    "financial": "finance",
    "bank": "finance",
    "press": "news",
    "webradio": "entertainment",
    "webtv": "entertainment",
    "games_online": "games",
    "search_engines": "search",
    "publicite": "ads_tracking",
    "educational_games": "education",
    "sect": "violence",
    "mixed_adult": "adult",
}


# High-volume consumer domains UT1 covers poorly. These are authoritative
# (loaded last, overriding UT1) and matched for the domain and all subdomains.
CURATED: dict[str, str] = {
    # social
    "facebook.com": "social", "fbcdn.net": "social", "instagram.com": "social",
    "cdninstagram.com": "social", "tiktok.com": "social", "tiktokcdn.com": "social",
    "twitter.com": "social", "x.com": "social", "twimg.com": "social",
    "snapchat.com": "social", "sc-cdn.net": "social", "reddit.com": "social",
    "redditmedia.com": "social", "pinterest.com": "social", "linkedin.com": "social",
    "threads.net": "social", "tumblr.com": "social", "bereal.com": "social",
    # messaging
    "whatsapp.com": "messaging", "whatsapp.net": "messaging", "telegram.org": "messaging",
    "t.me": "messaging", "discord.com": "messaging", "discordapp.com": "messaging",
    "discord.gg": "messaging", "messenger.com": "messaging", "signal.org": "messaging",
    # entertainment / streaming
    "youtube.com": "entertainment", "youtu.be": "entertainment", "ytimg.com": "entertainment",
    "googlevideo.com": "entertainment", "netflix.com": "entertainment",
    "nflxvideo.net": "entertainment", "nflximg.net": "entertainment",
    "spotify.com": "entertainment", "spotifycdn.com": "entertainment",
    "scdn.co": "entertainment", "twitch.tv": "entertainment", "ttvnw.net": "entertainment",
    "disneyplus.com": "entertainment", "primevideo.com": "entertainment",
    "dazn.com": "entertainment", "raiplay.it": "entertainment", "mediasetplay.it": "entertainment",
    "now.it": "entertainment", "soundcloud.com": "entertainment", "vimeo.com": "entertainment",
    "deezer.com": "entertainment",
    # games
    "roblox.com": "games", "rbxcdn.com": "games", "epicgames.com": "games",
    "steampowered.com": "games", "steamcommunity.com": "games", "minecraft.net": "games",
    "supercell.com": "games", "ea.com": "games", "playstation.com": "games",
    "xboxlive.com": "games", "nintendo.com": "games", "riotgames.com": "games",
    # education
    "wikipedia.org": "education", "wikimedia.org": "education", "khanacademy.org": "education",
    "duolingo.com": "education", "coursera.org": "education", "udemy.com": "education",
    "scuola.net": "education", "treccani.it": "education", "classroom.google.com": "education",
    "edmodo.com": "education", "weschool.com": "education", "zanichelli.it": "education",
    # news
    "repubblica.it": "news", "corriere.it": "news", "ilsole24ore.com": "news",
    "ansa.it": "news", "fanpage.it": "news", "ilfattoquotidiano.it": "news",
    "bbc.com": "news", "cnn.com": "news", "nytimes.com": "news",
    "finance.yahoo.com": "finance",
    # search
    "google.com": "search", "bing.com": "search", "duckduckgo.com": "search",
    "ecosia.org": "search", "qwant.com": "search",
    # shopping
    "amazon.it": "shopping", "amazon.com": "shopping", "ebay.it": "shopping",
    "zalando.it": "shopping", "aliexpress.com": "shopping", "shein.com": "shopping",
    "subito.it": "shopping", "vinted.it": "shopping",
    # messaging / webmail
    "gmail.com": "messaging", "mail.google.com": "messaging",
    "outlook.com": "messaging", "live.com": "messaging", "icloud.com": "messaging",
    # more education
    "google.com/scholar": "education", "scholar.google.com": "education",
    "geogebra.org": "education", "wordwall.net": "education", "quizlet.com": "education",
    "sololearn.com": "education", "ted.com": "education", "istruzione.it": "education",
    # more entertainment
    "crunchyroll.com": "entertainment", "plex.tv": "entertainment",
    "tv.apple.com": "entertainment", "paramountplus.com": "entertainment",
    "sky.it": "entertainment", "tim.it": "entertainment",
    # more games
    "fortnite.com": "games", "activision.com": "games", "blizzard.com": "games",
    "battle.net": "games", "twitch.tv": "entertainment",
    # dating (minor-sensitive)
    "tinder.com": "dating", "bumble.com": "dating", "badoo.com": "dating",
    "onlyfans.com": "adult",
}


# Domains tied to possible psychological distress. DNS can only reveal that a
# device contacted these hosts (never search terms). Two kinds are grouped here
# intentionally: harmful pro-self-harm/pro-ED communities AND help/crisis lines
# — visiting either is a signal worth a gentle, supportive check-in. This is a
# responsible STARTER list meant to be expanded.
SENSITIVE_DOMAINS: dict[str, str] = {
    # crisis / help lines (help-seeking signal)
    "telefonoamico.it": "self_harm", "azzurro.it": "self_harm",
    "samaritans.org": "self_harm", "befrienders.org": "self_harm",
    "988lifeline.org": "self_harm", "suicidepreventionlifeline.org": "self_harm",
    "crisistextline.org": "self_harm", "iasp.info": "self_harm",
    "hopeline.it": "self_harm", "progettoitaca.org": "self_harm",
    # pro-self-harm / pro-suicide communities (harmful)
    "sanctioned-suicide.net": "self_harm",
    # eating-disorder: help + harmful pro-ED
    "myproana.com": "eating_disorder", "bddanational.org": "eating_disorder",
    "nationaleatingdisorders.org": "eating_disorder", "disturbialimentari.it": "eating_disorder",
}


def seed_curated(conn: sqlite3.Connection) -> int:
    rows = [(d, c, "curated") for d, c in CURATED.items()]
    rows += [(d, c, "sensitive") for d, c in SENSITIVE_DOMAINS.items()]
    conn.executemany(
        "INSERT INTO domain_category(domain, category_key, source) VALUES(?,?,?) "
        "ON CONFLICT(domain) DO UPDATE SET category_key=excluded.category_key, "
        "source=excluded.source", rows)
    conn.commit()
    return len(rows)


def seed_categories(conn: sqlite3.Connection) -> None:
    for key, (label, risk, minor, color, critical) in CATEGORIES.items():
        conn.execute(
            "INSERT INTO category(key, label, risk_level, minor_sensitive, color, critical) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "label=excluded.label, risk_level=excluded.risk_level, "
            "minor_sensitive=excluded.minor_sensitive, color=excluded.color, "
            "critical=excluded.critical",
            (key, label, risk, minor, color, critical),
        )
    conn.commit()
