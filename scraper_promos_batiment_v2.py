"""
Scraper de promos bricolage/bâtiment — v2, extraction produit par produit.

Basé sur une structure RÉELLEMENT observée sur bonial.fr pendant cette
session (pages comme bonial.fr/Boulay-Moselle/Weldom/p-r27), qui liste
les promos en texte structuré : marque, nom du produit, prix actuel,
prix barré. Contrairement à v1, plus besoin d'OCR pour cette source.

Toujours à exécuter EN LOCAL (pas d'accès réseau dans cet environnement,
donc non testé en exécution réelle — la structure du HTML brut, avec ses
vraies classes CSS, n'a pas pu être inspectée directement). Le script
utilise deux stratégies d'extraction : une fiable basée sur les attributs
alt des images (confirmée manuellement), une plus fragile pour retrouver
le prix barré. Vérifie/ajuste au premier lancement.

AVANT DE L'UTILISER : vérifie les CGU de bonial.fr (bonial.fr/CGU) et
son robots.txt. Reste raisonnable en fréquence de requêtes (voir
DELAY_SECONDS) — usage personnel/prototype, pas d'usage commercial sans
accord préalable.

Installation :
    pip install requests beautifulsoup4 --break-system-packages

Usage :
    python scraper_promos_batiment_v2.py
"""

import re
import time
import json
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
DELAY_SECONDS = 3

# Villes couvrant le rayon 50 km autour de Porcelette (57890).
# À affiner via une vraie API géo (Google Places / Overpass) plutôt qu'à
# la main si tu veux couvrir le rayon exhaustivement.
VILLES = [
    "Boulay-Moselle",
    "Saint-Avold",
    "Forbach",
    "Sarreguemines",
    "Freyming-Merlebach",
]

PRICE_RE = re.compile(r"(\d+(?:,\d{2})?)\s?€")


# ---- Étape 1 : découvrir les enseignes bricolage présentes par ville ----

def get_enseignes_bricolage(ville: str) -> list[dict]:
    """Retourne les enseignes bricolage actives pour une ville, avec le
    lien vers leur page de promos individuelles.

    Page source : bonial.fr/{ville}/Bricolage/p-c1
    """
    url = f"https://www.bonial.fr/{ville}/Bricolage/p-c1"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    enseignes = []
    seen = set()
    # Les liens vers les pages promos par enseigne suivent le motif
    # /{ville}/{enseigne}/p-r{id}
    for a in soup.find_all("a", href=re.compile(rf"/{re.escape(ville)}/[^/]+/p-r\d+")):
        href = a.get("href")
        if href in seen:
            continue
        seen.add(href)
        enseignes.append({
            "ville": ville,
            "enseigne": href.split("/")[2],
            "url": href if href.startswith("http") else f"https://www.bonial.fr{href}",
        })
    return enseignes


# ---- Étape 2 : extraire les produits individuels d'une page promo ------

def get_produits(url: str, ville: str, enseigne: str) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    produits = []
    for img in soup.find_all("img", alt=re.compile(r"^Promo ")):
        alt = img.get("alt", "")
        # Motif confirmé : "Promo {nom} à {prix} € dans le catalogue ..."
        m = re.match(r"^Promo (.+?) à ([\d,]+)\s?€ dans le catalogue", alt)
        if not m:
            continue
        nom, prix_str = m.group(1), m.group(2)
        prix = float(prix_str.replace(",", "."))

        # Image miniature de la promo (sert de visuel produit).
        image_url = img.get("src") or img.get("data-src")
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url

        # Lien cliquable : l'image est généralement entourée d'un <a>
        # pointant vers la fiche/page exacte de la promo dans le
        # catalogue. On remonte jusqu'au premier <a> trouvé.
        lien = None
        ancre = img.find_parent("a")
        if ancre and ancre.get("href"):
            href = ancre["href"]
            lien = href if href.startswith("http") else f"https://www.bonial.fr{href}"

        # Prix barré : cherche dans le texte du bloc englobant (les 2-3
        # parents au-dessus de l'image), en excluant le prix déjà trouvé.
        # Fragile — à ajuster selon le vrai DOM.
        prix_barre = None
        node = img
        for _ in range(3):
            if node.parent is None:
                break
            node = node.parent
        bloc_texte = node.get_text(" ", strip=True)
        prix_trouves = [float(p.replace(",", ".")) for p in PRICE_RE.findall(bloc_texte)]
        autres_prix = [p for p in prix_trouves if abs(p - prix) > 0.01]
        if autres_prix:
            prix_barre = max(autres_prix)  # le prix barré est le plus élevé

        produits.append({
            "ville": ville,
            "enseigne": enseigne,
            "produit": nom,
            "prix": prix,
            "prix_barre": prix_barre,
            "remise_pct": round((1 - prix / prix_barre) * 100) if prix_barre else None,
            "image_url": image_url,
            "lien": lien,
        })
    return produits


import os


def charger_besoins() -> list[dict]:
    """Charge needs.json — la liste des matériaux recherchés."""
    try:
        with open("needs.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def correspond_a_un_besoin(nom_produit: str, besoins: list[dict]) -> dict | None:
    """Retourne le besoin correspondant si le nom du produit contient
    tous les mots-clés d'un des besoins de la liste, sinon None."""
    n = nom_produit.lower()
    for besoin in besoins:
        if all(mot.lower() in n for mot in besoin["mots_cles"]):
            return besoin
    return None


def envoyer_alerte_ntfy(produits_matches: list[dict]) -> None:
    """Envoie une notification push via ntfy.sh pour les produits qui
    correspondent à un besoin. Nécessite la variable d'environnement
    NTFY_TOPIC (définie comme secret GitHub Actions — voir le workflow).
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or not produits_matches:
        return

    lignes = [
        f"• {p['produit']} — {p['prix']:.2f} € ({p['enseigne']}, {p['ville']})"
        for p in produits_matches[:10]  # ntfy limite la taille du message
    ]
    message = "\n".join(lignes)
    titre = f"{len(produits_matches)} promo(s) correspondant à tes besoins"

    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": titre.encode("utf-8"), "Priority": "default"},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Échec de l'envoi de la notification : {e}")


# ---- Boucle principale --------------------------------------------------

def main():
    tous_produits = []
    for ville in VILLES:
        print(f"→ {ville}")
        try:
            enseignes = get_enseignes_bricolage(ville)
        except requests.HTTPError as e:
            print(f"  Échec liste enseignes ({e})")
            continue
        print(f"  {len(enseignes)} enseigne(s) bricolage trouvée(s)")
        time.sleep(DELAY_SECONDS)

        for ens in enseignes:
            print(f"    {ens['enseigne']}…", end=" ")
            try:
                produits = get_produits(ens["url"], ville, ens["enseigne"])
                tous_produits.extend(produits)
                print(f"{len(produits)} produit(s)")
            except requests.HTTPError as e:
                print(f"échec ({e})")
            time.sleep(DELAY_SECONDS)

    # Recoupement avec la liste de besoins
    besoins = charger_besoins()
    matches = []
    for p in tous_produits:
        besoin = correspond_a_un_besoin(p["produit"], besoins)
        p["correspond_besoin"] = besoin["note"] if besoin else None
        if besoin:
            matches.append(p)

    if matches:
        print(f"\n{len(matches)} produit(s) correspondent à un besoin — envoi de l'alerte")
        envoyer_alerte_ntfy(matches)

    sortie = {
        "genere_le": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "produits": tous_produits,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=2)

    print(f"\n{len(tous_produits)} produits au total → data.json")


if __name__ == "__main__":
    main()
