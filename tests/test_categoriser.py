#!/usr/bin/env python3.8
# -*- coding: utf-8 -*-
"""tests/test_categoriser.py — garde-fou anti-régression de la catégorisation multilingue.

    Lancer :   python3.8 tests/test_categoriser.py
    (ou pytest : pytest tests/test_categoriser.py)

Deux propriétés vérifiées sur analytics.categoriser :

  1. PIÈGES — aucun faux positif inter-langue. Le bug corrigé le 2026-08-03 venait
     du substring brut (« m in q ») : un mot-clé était détecté AU MILIEU d'un autre
     mot (« eau » dans « beaucoup », « room » dans « restroom », « porte » dans
     « apporter », « bus » dans « business »…). Le correctif ancre les mots-clés
     latins sur un début de mot (\\b). Si quelqu'un rajoute un mot-clé à
     _KEYWORD_ZONE et réintroduit une telle collision, ces cas échouent.

  2. POSITIFS — la bonne zone est bien trouvée dans les 8 langues supportées
     (fr, en, es, de, it, zh, ja, ar), y compris via les préfixes voulus
     (« embarq » → « embarquement », « aide » → « aidez »).

Chaque cas est (phrase, {catégories acceptées}). L'ensemble autorise les rares
ambiguïtés légitimes (ex. « nouveau billet » n'a aucun mot-clé → « Autre »).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analytics import categoriser  # noqa: E402


# ── 1. PIÈGES : ces phrases ne doivent JAMAIS tomber dans la mauvaise zone ──────
PIEGES = [
    ("merci beaucoup",                       {"Autre"}),                 # eau ⊄ beaucoup
    ("je voudrais un nouveau billet",        {"Autre"}),                 # eau ⊄ nouveau
    ("où est le bureau d'information",       {"Lieux & itinéraires"}),   # eau ⊄ bureau
    ("where is the restroom",                {"Services pratiques"}),    # room ⊄ restroom
    ("where is the bathroom",                {"Services pratiques"}),    # room ⊄ bathroom
    ("pouvez-vous m'apporter un café",       {"Restauration"}),          # porte ⊄ apporter
    ("quelle bonne surprise",                {"Autre"}),                 # prise ⊄ surprise
    ("je cherche une personne",              {"Autre"}),                 # perso ⊄ personne
    ("that was a great trip",                {"Autre"}),                 # eat ⊄ great
    ("can you navigate me there",            {"Autre"}),                 # gate ⊄ navigate
    ("I'm here on business",                 {"Autre"}),                 # bus ⊄ business
    ("where is the business lounge",         {"Lieux & itinéraires"}),   # bus ⊄ business (→ "where is")
]


# ── 2. POSITIFS : la bonne zone détectée dans chaque langue ─────────────────────
POSITIFS = [
    # Vols
    ("où est ma porte d'embarquement",       {"Vols"}),
    ("is my flight on time",                 {"Vols"}),
    ("¿cuál es la puerta de embarque?",      {"Vols"}),
    ("wo ist mein flug nach berlin",         {"Vols"}),
    ("a che ora è l'imbarco",                {"Vols"}),
    ("航班信息",                              {"Vols"}),
    ("搭乗ゲートはどこですか",                  {"Vols"}),
    ("أين بوابة الصعود",                      {"Vols"}),
    # Hôtels
    ("un hôtel pas cher près d'ici",         {"Hôtels"}),
    ("I need a cheap hotel",                  {"Hôtels"}),
    ("quiero una habitación para esta noche", {"Hôtels"}),
    ("ich brauche ein zimmer",               {"Hôtels"}),
    ("cerco una camera per la notte",        {"Hôtels"}),
    ("我需要一个酒店",                         {"Hôtels"}),
    ("ホテルを探しています",                    {"Hôtels"}),
    ("أبحث عن فندق",                          {"Hôtels"}),
    # Transport
    ("où prendre le rer b",                  {"Transport"}),
    ("where is the taxi stand",              {"Transport"}),
    ("¿dónde está el metro?",                {"Transport"}),
    ("wo ist der zug nach paris",            {"Transport"}),
    ("dov'è il treno",                       {"Transport"}),
    ("出租车在哪里",                           {"Transport"}),
    ("タクシーはどこですか",                    {"Transport"}),
    ("أين الحافلة",                          {"Transport"}),
    # Bagages
    ("j'ai perdu ma valise",                 {"Bagages"}),
    ("I lost my luggage",                    {"Bagages"}),
    ("he perdido mi equipaje",               {"Bagages"}),
    ("mein gepäck ist weg",                  {"Bagages"}),
    ("ho perso la valigia",                  {"Bagages"}),
    ("我的行李丢了",                           {"Bagages"}),
    ("荷物をなくしました",                      {"Bagages"}),
    ("لقد فقدت حقيبة",                        {"Bagages"}),
    # Restauration
    ("où puis-je manger",                    {"Restauration"}),
    ("where can I get a coffee",             {"Restauration"}),
    ("quiero comer algo",                    {"Restauration"}),
    ("wo kann ich essen",                    {"Restauration"}),
    ("dove posso mangiare",                  {"Restauration"}),
    ("哪里有餐厅",                            {"Restauration"}),
    ("レストランはどこ",                       {"Restauration"}),
    ("أين مطعم",                             {"Restauration"}),
    # Services pratiques
    ("où sont les toilettes",                {"Services pratiques"}),
    ("I need some water",                    {"Services pratiques"}),
    ("¿dónde están los baños?",              {"Services pratiques"}),
    ("wo ist die toilette",                  {"Services pratiques"}),
    ("dove sono i bagni",                    {"Services pratiques"}),
    ("厕所在哪里",                            {"Services pratiques"}),
    ("トイレはどこですか",                     {"Services pratiques"}),
    ("أين الحمام",                           {"Services pratiques"}),
    # Assistance humaine
    ("j'ai besoin d'un fauteuil roulant",    {"Assistance humaine"}),
    ("I need help please",                   {"Assistance humaine"}),
    ("necesito una silla de ruedas",         {"Assistance humaine"}),
    ("ich brauche hilfe",                    {"Assistance humaine"}),
    ("ho bisogno di aiuto",                  {"Assistance humaine"}),
    ("我需要帮助",                            {"Assistance humaine"}),
    ("助けてください",                         {"Assistance humaine"}),
    ("أحتاج مساعدة",                         {"Assistance humaine"}),
    # Lieux & itinéraires
    ("comment aller à la sortie",            {"Lieux & itinéraires"}),
    ("how to get to the exit",               {"Lieux & itinéraires"}),
    ("¿cómo llegar a la salida?",            {"Lieux & itinéraires"}),
    ("wie komme ich zum ausgang",            {"Lieux & itinéraires"}),
    ("come arrivare all'uscita",             {"Lieux & itinéraires"}),
    ("出口在哪里",                            {"Lieux & itinéraires"}),
    ("出口はどこですか",                       {"Lieux & itinéraires"}),
    ("أين المخرج",                           {"Lieux & itinéraires"}),
    # Wi-Fi & services
    ("il y a du wifi ici",                   {"Wi-Fi & services"}),
    ("where can I charge my phone",          {"Wi-Fi & services"}),
    ("necesito cargar el móvil",             {"Wi-Fi & services"}),
    ("wo kann ich mein handy laden",         {"Wi-Fi & services"}),
    ("dove posso ricaricare",                {"Wi-Fi & services"}),
    ("哪里可以充电",                          {"Wi-Fi & services"}),
    ("充電できますか",                         {"Wi-Fi & services"}),
    ("أريد شحن هاتفي",                        {"Wi-Fi & services"}),
]


def _echecs(cas):
    """Retourne la liste des (phrase, obtenu, attendu) qui ne matchent pas."""
    bad = []
    for phrase, attendu in cas:
        got = categoriser(phrase)
        if got not in attendu:
            bad.append((phrase, got, attendu))
    return bad


# ── Interface pytest ────────────────────────────────────────────────────────────
def test_pas_de_conflit_de_langue():
    bad = _echecs(PIEGES)
    assert not bad, "Conflits inter-langue : " + " | ".join(
        "{!r}→{} (attendu {})".format(p, g, sorted(a)) for p, g, a in bad)


def test_detection_dans_les_8_langues():
    bad = _echecs(POSITIFS)
    assert not bad, "Détections ratées : " + " | ".join(
        "{!r}→{} (attendu {})".format(p, g, sorted(a)) for p, g, a in bad)


# ── Exécution directe (sans pytest) ──────────────────────────────────────────────
if __name__ == "__main__":
    total = 0
    for titre, cas in [("PIÈGES — faux positifs à éviter", PIEGES),
                       ("POSITIFS — détection dans les 8 langues", POSITIFS)]:
        bad = _echecs(cas)
        total += len(bad)
        print("\n=== {} ===  ({}/{} OK)".format(titre, len(cas) - len(bad), len(cas)))
        for phrase, attendu in cas:
            got = categoriser(phrase)
            ok = got in attendu
            if not ok:
                print("  ❌ {!r}\n       obtenu {} — attendu {}".format(phrase, got, sorted(attendu)))
        if not bad:
            print("  ✅ tout passe")
    print("\n" + ("✅ TOUT VERT — {} cas".format(len(PIEGES) + len(POSITIFS))
                  if total == 0 else "❌ {} cas en échec".format(total)))
    sys.exit(1 if total else 0)
