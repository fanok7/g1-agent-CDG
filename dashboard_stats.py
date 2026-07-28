"""dashboard_stats.py — Dashboard de télémétrie du robot d'accueil G1 (Streamlit).

Visualise les statistiques anonymes (questions, langues, sujets, satisfaction,
appels, alertes, santé). Mode d'emploi complet : voir DASHBOARD.md.

(NB : nommé dashboard_stats.py et non dashboard.py — ce dernier existe déjà et
est le superviseur ESP32, à ne pas écraser.)

Lancement — le plus simple (Streamlit + pandas + openpyxl sont installés sur le
robot ; on le consulte depuis le navigateur du PC) :

    cd /home/unitree/g1_agent_interim
    bash scripts/lancer_dashboard.sh        # headless auto si pas d'écran
    # → ouvrir http://192.168.123.164:8501 sur le PC

Sur le PC directement : `pip install streamlit pandas openpyxl` puis la même
commande (ouvre le navigateur tout seul).
"""

import io
import json
import os
import urllib.request

import pandas as pd
import streamlit as st

# Chemin local tenté par défaut (si le dashboard tourne à côté du fichier).
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tablet_server", "analytics_events.jsonl")
# URL du robot (surchargée par la variable d'env G1_ROBOT_URL).
_ROBOT_URL = os.environ.get("G1_ROBOT_URL", "http://192.168.123.164:8000") + "/api/export-logs"


def recuperer_depuis_robot(url):
    """Télécharge le journal directement depuis le robot (une seule commande)."""
    with urllib.request.urlopen(url, timeout=8) as r:
        return r.read().decode("utf-8", errors="replace")

LANG_NOMS = {"fr": "Français", "en": "Anglais", "es": "Espagnol", "de": "Allemand",
             "it": "Italien", "ar": "Arabe", "zh": "Chinois", "ja": "Japonais"}

# En-têtes en français pour l'export CSV LISIBLE (Excel). Le CSV se ré-uploade :
# à la lecture on retraduit ces en-têtes vers les noms internes. Les VALEURS de la
# colonne "type" restent en anglais (elles servent aux filtres du dashboard).
COLS_FR = {
    "ts": "Horodatage", "session_id": "Session", "type": "Type", "category": "Catégorie",
    "lang": "Langue", "q_anonymized": "Question", "latency_ms": "Latence (ms)",
    "is_fallback": "Sans réponse directe", "tool_name": "Outil", "status": "Statut",
    "note": "Note", "motif": "Motif", "answered": "Répondu", "duration_s": "Durée (s)",
    "kind": "Alerte", "cpu_percent": "CPU %", "ram_percent": "RAM %",
    "usb_connected": "Tablette connectée", "api_errors_count": "Erreurs API",
}
FR_VERS_INTERNE = {v: k for k, v in COLS_FR.items()}

# Excel LISIBLE : un onglet par type d'événement, avec SEULEMENT ses colonnes
# utiles (fini les cellules vides d'un grand tableau unique). (onglet, type interne,
# colonnes internes affichées).
ONGLETS = [
    ("Questions", "question", ["ts", "session_id", "category", "lang", "q_anonymized",
                               "latency_ms", "is_fallback"]),
    ("Services",  "tool",     ["ts", "session_id", "category", "tool_name", "status"]),
    ("Avis",      "rating",   ["ts", "session_id", "category", "note"]),
    ("Appels",    "call",     ["ts", "session_id", "motif", "answered", "duration_s"]),
    ("Alertes",   "alert",    ["ts", "kind"]),
    ("Santé",     "health",   ["ts", "cpu_percent", "ram_percent", "usb_connected",
                               "api_errors_count"]),
]
_ONGLET_VERS_TYPE = {nom: typ for nom, typ, _ in ONGLETS}


def construire_excel(df):
    """Renvoie un classeur Excel (bytes) : un onglet clair par type d'événement."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for nom, typ, cols in ONGLETS:
            sub = df[df["type"] == typ]
            cols = [c for c in cols if c in sub.columns]
            if sub.empty or not cols:
                continue
            sub = sub[cols].rename(columns=COLS_FR)
            sub.to_excel(xl, sheet_name=nom, index=False)
            # Largeurs de colonnes lisibles.
            ws = xl.sheets[nom]
            for i, col in enumerate(sub.columns, start=1):
                largeur = max(len(str(col)), *(sub[col].astype(str).map(len).tolist() or [0]))
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(largeur + 2, 50)
    return buf.getvalue()


def lire_excel(octets):
    """Relit un classeur Excel exporté → DataFrame (concatène les onglets)."""
    feuilles = pd.read_excel(io.BytesIO(octets), sheet_name=None)
    morceaux = []
    for nom, sous in feuilles.items():
        sous = sous.rename(columns=FR_VERS_INTERNE)
        sous["type"] = _ONGLET_VERS_TYPE.get(nom, nom.lower())
        morceaux.append(sous)
    return pd.concat(morceaux, ignore_index=True) if morceaux else pd.DataFrame()


def df_vers_jsonl(df):
    """Sérialise un DataFrame en JSONL (une ligne = un événement, sans les NaN)."""
    out = []
    for _, row in df.iterrows():
        d = {k: v for k, v in row.items() if pd.notna(v) and not str(k).startswith("_")}
        out.append(json.dumps(d, ensure_ascii=False, default=str))
    return "\n".join(out)


st.set_page_config(page_title="Robot G1 — Télémétrie", page_icon="📊", layout="wide")


# ── Chargement des données ──────────────────────────────────────────────────
def charger_donnees(contenu):
    """Construit un DataFrame depuis du JSONL OU du CSV (les deux formats que
    ce dashboard exporte). Détection automatique par le 1er caractère utile."""
    txt = (contenu or "").lstrip()
    if txt.startswith("{"):
        # JSONL : une ligne = un objet JSON.
        lignes = []
        for ligne in contenu.splitlines():
            ligne = ligne.strip()
            if ligne:
                try:
                    lignes.append(json.loads(ligne))
                except json.JSONDecodeError:
                    pass
        df = pd.DataFrame(lignes)
    else:
        # CSV (export lisible) : on retraduit les en-têtes français → internes.
        df = pd.read_csv(io.StringIO(contenu))
        df = df.rename(columns=FR_VERS_INTERNE)

    # Types cohérents quel que soit le format (le CSV renvoie du texte).
    for col in ("note", "latency_ms", "duration_s", "cpu_percent",
                "ram_percent", "api_errors_count"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("is_fallback", "answered", "usb_connected"):
        if col in df:
            df[col] = df[col].map({True: True, False: False, "True": True,
                                   "False": False, "true": True, "false": False})
    return df


st.title("📊 Robot d'accueil G1 — Télémétrie")
st.caption("Statistiques anonymes — aucune donnée personnelle (RGPD)")

# ── Choix de la source (barre latérale) ────────────────────────────────────
st.sidebar.header("Source des données")
url_robot = st.sidebar.text_input("URL du robot", _ROBOT_URL)
rafraichir = st.sidebar.button("🔄 Récupérer depuis le robot")
fichier = st.sidebar.file_uploader("…ou déposer un fichier (`.xlsx`, `.csv` ou `.jsonl`)",
                                    type=["xlsx", "csv", "jsonl", "json", "txt"])

# Priorité : bouton robot > fichier déposé > cache mémoire > fichier local.
contenu_brut = None
if rafraichir:
    try:
        contenu_brut = recuperer_depuis_robot(url_robot)
        st.session_state["data"] = contenu_brut
        st.sidebar.success("Données récupérées depuis le robot ✅")
    except Exception as e:
        st.sidebar.error("Robot injoignable : {}\n(le robot est-il allumé ?)".format(e))
elif fichier is not None:
    if fichier.name.lower().endswith(".xlsx"):
        # Fichier Excel (binaire) → on le relit et on le reconvertit en JSONL
        # texte pour rester cohérent avec le reste du flux.
        try:
            contenu_brut = df_vers_jsonl(lire_excel(fichier.getvalue()))
        except Exception as e:
            st.sidebar.error("Excel illisible : {}".format(e))
    else:
        contenu_brut = fichier.getvalue().decode("utf-8", errors="replace")
    if contenu_brut:
        st.session_state["data"] = contenu_brut

if contenu_brut is None:
    contenu_brut = st.session_state.get("data")          # garde la dernière récup
if contenu_brut is None and os.path.exists(_DEFAULT_PATH):
    with open(_DEFAULT_PATH, encoding="utf-8") as f:      # secours : fichier voisin
        contenu_brut = f.read()
    st.info("Fichier local chargé : `{}`".format(_DEFAULT_PATH))

if not contenu_brut:
    st.warning("Aucune donnée. Clique « 🔄 Récupérer depuis le robot » dans la "
               "barre latérale (robot allumé), ou dépose un fichier `.jsonl`.")
    st.stop()

df = charger_donnees(contenu_brut)
if df.empty or "type" not in df.columns:
    st.warning("Le fichier ne contient aucun événement exploitable.")
    st.stop()

# Sous-ensembles par type d'événement.
q = df[df["type"] == "question"]
tools = df[df["type"] == "tool"]
ratings = df[df["type"] == "rating"]
calls = df[df["type"] == "call"]
alerts = df[df["type"] == "alert"]

# Colonnes temps dérivées (jour + heure) pour l'activité et l'affluence.
_dt = pd.to_datetime(df.get("ts"), errors="coerce")
df = df.assign(_jour=_dt.dt.date.astype(str), _heure=_dt.dt.hour)


# ── KPI globaux (vue d'ensemble complète) ───────────────────────────────────
nb_sessions = df["session_id"].dropna().nunique() if "session_id" in df else 0
note_moy = round(ratings["note"].mean(), 2) if not ratings.empty else None
taux_fallback = (round(100.0 * q["is_fallback"].fillna(False).sum() / len(q), 1)
                 if not q.empty and "is_fallback" in q else 0.0)
zone_top = (tools["category"].value_counts().idxmax()
            if not tools.empty and "category" in tools else "—")
lat_moy = (int(q["latency_ms"].dropna().mean())
           if not q.empty and "latency_ms" in q and q["latency_ms"].notna().any() else None)
jour_actif = (df["_jour"].value_counts().idxmax()
              if df["_jour"].notna().any() else "—")

l1 = st.columns(4)
l1[0].metric("Sessions (visites)", nb_sessions)
l1[1].metric("Note moyenne", "{} ★".format(note_moy) if note_moy is not None else "—")
l1[2].metric("Taux de fallback IA", "{} %".format(taux_fallback),
             help="Part des questions où l'IA n'a pas su répondre directement.")
l1[3].metric("Latence moy. réponse", "{} ms".format(lat_moy) if lat_moy is not None else "—")

l2 = st.columns(4)
l2[0].metric("Questions posées", len(q))
l2[1].metric("Appels d'assistance 🆘", len(calls))
l2[2].metric("Alertes sécurité 🚨", len(alerts))
l2[3].metric("Zone la + sollicitée", zone_top)

st.caption("Interactions totales : {} · Jour le plus actif : {}".format(len(df), jour_actif))
st.divider()


# ── Sujets & services ───────────────────────────────────────────────────────
g1, g2 = st.columns(2)
with g1:
    st.subheader("🧭 Sujets des questions")
    if not q.empty and "category" in q:
        st.bar_chart(q["category"].value_counts(), color="#7B2CBF", horizontal=True)
    else:
        st.caption("Pas encore de questions.")
with g2:
    st.subheader("🛠️ Services réellement utilisés")
    if not tools.empty and "category" in tools:
        st.bar_chart(tools["category"].value_counts(), color="#38B000", horizontal=True)
    else:
        st.caption("Aucun service appelé pour l'instant.")


# ── Langues & fréquentation ─────────────────────────────────────────────────
g3, g4 = st.columns(2)
with g3:
    st.subheader("🗣️ Langues des visiteurs")
    if not q.empty and "lang" in q:
        st.bar_chart(q["lang"].map(lambda c: LANG_NOMS.get(c, c)).value_counts(), color="#9D4EDD")
    else:
        st.caption("Pas encore de données de langue.")
with g4:
    st.subheader("📅 Activité par jour")
    if df["_jour"].notna().any():
        st.line_chart(df["_jour"].value_counts().sort_index(), color="#5A189A")
    else:
        st.caption("Pas encore d'activité datée.")

st.subheader("🕐 Affluence par heure (heures de pointe)")
if df["_heure"].notna().any():
    par_heure = df["_heure"].value_counts().reindex(range(24), fill_value=0).sort_index()
    par_heure.index = ["{}h".format(h) for h in range(24)]
    st.bar_chart(par_heure, color="#7B2CBF")
else:
    st.caption("Pas encore d'activité horodatée.")

st.divider()


# ── Satisfaction ────────────────────────────────────────────────────────────
st.subheader("⭐ Satisfaction")
if not ratings.empty:
    s1, s2 = st.columns(2)
    with s1:
        st.caption("Répartition des notes")
        rep = ratings["note"].value_counts().reindex([1, 2, 3, 4, 5], fill_value=0)
        rep.index = ["{}★".format(i) for i in [1, 2, 3, 4, 5]]
        st.bar_chart(rep, color="#7B2CBF")
    with s2:
        st.caption("Satisfaction moyenne par zone (repère un sujet mal traité)")
        parz = (ratings.groupby("category")["note"]
                .agg(["mean", "count"]).sort_values("mean"))
        parz.columns = ["Note moyenne", "Nb avis"]
        parz["Note moyenne"] = parz["Note moyenne"].round(2)
        st.dataframe(parz, width="stretch")
        pire = parz.index[0]
        if parz["Note moyenne"].iloc[0] <= 2.5:
            st.error("⚠️ Zone la moins bien notée : **{}** ({} ★)".format(
                pire, parz["Note moyenne"].iloc[0]))
else:
    st.caption("Aucun avis pour le moment.")

st.divider()


# ── Questions fréquentes ────────────────────────────────────────────────────
st.subheader("❓ Questions les plus fréquentes")
if not q.empty and "q_anonymized" in q:
    freq = (q["q_anonymized"].value_counts().head(15).rename_axis("Question (anonymisée)")
            .reset_index(name="Occurrences"))
    st.dataframe(freq, width="stretch", hide_index=True)
else:
    st.caption("Aucune question enregistrée.")

st.divider()


# ── Appels à l'humain & alertes ─────────────────────────────────────────────
st.subheader("🆘 Derniers appels à l'humain")
if not calls.empty:
    cols = [c for c in ["ts", "motif", "answered", "duration_s"] if c in calls.columns]
    vue = calls[cols].rename(columns={"ts": "Heure", "motif": "Motif",
                                      "answered": "Répondu", "duration_s": "Durée (s)"})
    st.dataframe(vue.iloc[::-1], width="stretch", hide_index=True)
else:
    st.caption("Aucun appel d'assistance enregistré.")

st.subheader("🚨 Alertes sécurité")
if not alerts.empty:
    cols = [c for c in ["ts", "kind"] if c in alerts.columns]
    va = alerts[cols].rename(columns={"ts": "Heure", "kind": "Type"})
    st.dataframe(va.iloc[::-1], width="stretch", hide_index=True)
else:
    st.success("Aucune alerte détectée. ✅")

st.divider()


# ── Téléchargement ──────────────────────────────────────────────────────────
st.subheader("⬇️ Exporter les données")
st.caption("L'Excel s'ouvre en clair : **un onglet par type** (Questions, Services, "
           "Avis, Appels, Alertes, Santé), chacun sans colonne vide.")
d1, d2 = st.columns(2)
d1.download_button(
    label="📗 Excel lisible (.xlsx)",
    data=construire_excel(df),
    file_name="statistiques_robot_g1.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    help="Un onglet par type d'événement, colonnes en français. Ouvre directement dans Excel.",
)
d2.download_button(
    label="📄 Journal brut (.jsonl)",
    data=contenu_brut.encode("utf-8"),
    file_name="analytics_events.jsonl",
    mime="application/x-ndjson",
    help="Format technique d'origine — sert à recharger les données ici.",
)
