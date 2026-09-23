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
import os
import unicodedata
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
    "Metz",
    "Augny",
]

PRICE_RE = re.compile(r"(\d+(?:,\d{2})?)\s?€")
VALIDITE_RE = re.compile(r"[Vv]alable jusqu['’]au (\d{2}/\d{2}/\d{4})")
ENSEIGNE_RE = re.compile(r"/([^/]+)/p-r\d+")


def slugify(terme: str) -> str:
    """Convertit un mot-clé en slug de catégorie Bonial : sans accents,
    mots séparés par des tirets, chaque mot avec une majuscule initiale
    (ex. 'câble électrique' -> 'Cable-Electrique'). Ne garantit pas que
    la catégorie existe réellement chez Bonial — voir get_promos_produit.
    """
    nfkd = unicodedata.normalize("NFKD", terme)
    sans_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    mots = re.split(r"[\s-]+", sans_accents.strip())
    return "-".join(m.capitalize() for m in mots if m)


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
    # /{ville}/{enseigne}/p-r{id}. On extrait le nom de l'enseigne via
    # regex sur le href plutôt que par position (fragile si le lien est
    # absolu au lieu de relatif — bug corrigé ici).
    for a in soup.find_all("a", href=re.compile(rf"/{re.escape(ville)}/[^/]+/p-r\d+")):
        href = a.get("href")
        if href in seen:
            continue
        seen.add(href)
        m = ENSEIGNE_RE.search(href)
        nom_enseigne = m.group(1).replace("-", " ") if m else "Enseigne inconnue"
        enseignes.append({
            "ville": ville,
            "enseigne": nom_enseigne,
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
            "valide_jusquau": None,
            "type": "produit",
        })

    # Repli : certaines enseignes (observé pour Brico Dépôt, Leroy Merlin)
    # n'exposent pas de promos individuelles en texte structuré sur cette
    # page — seulement une carte de catalogue avec une période de
    # validité. Dans ce cas, on remonte au moins une entrée "catalogue
    # complet" plutôt que de ne rien montrer du tout. Couvrir chaque
    # produit de ces catalogues nécessiterait de l'OCR sur le prospectus
    # (non fait ici).
    if not produits:
        texte_page = soup.get_text(" ", strip=True)
        m_date = VALIDITE_RE.search(texte_page)
        date_validite = m_date.group(1) if m_date else None

        img_catalogue = soup.find("img", alt=re.compile(r"^(Prospectus|Catalogue)"))
        image_url = None
        if img_catalogue:
            image_url = img_catalogue.get("src") or img_catalogue.get("data-src")
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url

        if date_validite or img_catalogue:
            produits.append({
                "ville": ville,
                "enseigne": enseigne,
                "produit": f"Catalogue {enseigne} — voir toutes les promos",
                "prix": None,
                "prix_barre": None,
                "remise_pct": None,
                "image_url": image_url,
                "lien": url,
                "valide_jusquau": date_validite,
                "type": "catalogue",
            })

    return produits


# ---- Étape 3 : recherche ciblée par produit, toutes enseignes -----------
# Source distincte, plus fiable : bonial.fr/{ville}/Promos/{terme} donne
# un vrai tableau (Produit / Marque / Enseigne / Prix / Remise) agrégeant
# TOUTES les enseignes pour ce terme — contourne le problème des pages
# par enseigne qui ratent certains magasins (Castorama notamment).
# Limite connue : le terme doit correspondre à une catégorie Bonial
# existante (ex. "Carrelage-Mural" fonctionne, un slug inventé peut ne
# renvoyer aucun résultat sans que ce soit une erreur).

def get_promos_produit(ville: str, terme: str) -> list[dict]:
    slug = slugify(terme)
    url = f"https://www.bonial.fr/{ville}/Promos/{slug}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table")
    if not table:
        return []

    produits = []
    lignes = table.find_all("tr")[1:]  # ignore l'en-tête
    for ligne in lignes:
        cols = [c.get_text(" ", strip=True) for c in ligne.find_all(["td", "th"])]
        if len(cols) < 4:
            continue
        nom, marque, enseigne, prix_col = cols[0], cols[1], cols[2], cols[3]
        remise_col = cols[5] if len(cols) > 5 else ""

        m_prix = PRICE_RE.search(prix_col)
        if not m_prix:
            continue
        prix = float(m_prix.group(1).replace(",", "."))

        prix_barre = None
        m_remise = PRICE_RE.search(remise_col) if remise_col else None
        if m_remise:
            prix_barre = prix + float(m_remise.group(1).replace(",", "."))

        nom_complet = nom if not marque or marque.lower() in nom.lower() else f"{marque} {nom}"

        produits.append({
            "ville": ville,
            "enseigne": enseigne,
            "produit": nom_complet.strip(),
            "prix": prix,
            "prix_barre": prix_barre,
            "remise_pct": round((1 - prix / prix_barre) * 100) if prix_barre else None,
            "image_url": None,
            "lien": url,
            "valide_jusquau": None,
            "type": "produit",
        })
    return produits


# ---- Étape 4 : catalogues allemands (Sarrelouis / Ensdorf) --------------
# Contrairement à bonial.fr, prospektangebote.de (l'équivalent allemand)
# n'expose PAS de tableau produit par produit pour Globus Baumarkt et
# Bauhaus — uniquement un prospectus en pages-images, comme pour les
# enseignes françaises sans données structurées. Donc : pas d'alerte
# possible par mot-clé ici, seulement une carte "voir le prospectus".
# Slugs vérifiés en direct : globus-baumarkt + saarlouis. Le reste
# (bauhaus, ensdorf-saarlouis) est déduit par convention, à confirmer au
# premier run.

ENSEIGNES_ALLEMAGNE = ["globus-baumarkt", "bauhaus"]
VILLES_ALLEMAGNE = ["saarlouis", "ensdorf-saarlouis"]

VALIDITE_DE_RE = re.compile(r"Gültig von ([^<\n]+?) bis ([^<\n.]+)")


def get_prospekt_allemagne(enseigne_slug: str, ville_slug: str) -> list[dict]:
    url = f"https://www.prospektangebote.de/geschaefte/{enseigne_slug}/standorte/{ville_slug}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    lien_prospectus = None
    for a in soup.find_all("a", href=re.compile(r"/anzeigen/angebote/")):
        lien_prospectus = a["href"]
        if lien_prospectus.startswith("/"):
            lien_prospectus = f"https://www.prospektangebote.de{lien_prospectus}"
        break
    if not lien_prospectus:
        return []  # pas de prospectus actif pour cette enseigne/ville

    texte_page = soup.get_text(" ", strip=True)
    m_validite = VALIDITE_DE_RE.search(texte_page)
    validite = f"{m_validite.group(1)} – {m_validite.group(2)}".strip() if m_validite else None

    logo = soup.find("img", alt=re.compile(re.escape(enseigne_slug.replace("-", " ")), re.I))
    image_url = logo.get("src") if logo else None

    nom_enseigne = enseigne_slug.replace("-", " ").title()
    return [{
        "ville": f"{ville_slug} (DE)",
        "enseigne": nom_enseigne,
        "produit": f"Prospectus {nom_enseigne} — voir toutes les promos (Allemagne)",
        "prix": None,
        "prix_barre": None,
        "remise_pct": None,
        "image_url": image_url,
        "lien": lien_prospectus,
        "valide_jusquau": validite,
        "type": "catalogue",
    }]


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
    besoins = charger_besoins()

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

    # Recherche ciblée par besoin — un terme par entrée de needs.json,
    # interrogé pour chaque ville. Vient compléter (pas remplacer) le
    # scraping par enseigne ci-dessus, avec une bien meilleure couverture
    # toutes enseignes confondues.
    termes = sorted({b["mots_cles"][0] for b in besoins if b.get("mots_cles")})
    if termes:
        print(f"\nRecherche ciblée pour {len(termes)} terme(s) de besoin :")
        for ville in VILLES:
            for terme in termes:
                try:
                    produits = get_promos_produit(ville, terme)
                    if produits:
                        print(f"  '{terme}' à {ville} : {len(produits)} résultat(s)")
                    tous_produits.extend(produits)
                except requests.HTTPError:
                    pass
                time.sleep(DELAY_SECONDS)

    # Prospectus allemands (Sarrelouis / Ensdorf) — catalogues complets
    # uniquement, pas de produits individuels (voir note plus haut).
    print(f"\nProspectus allemands :")
    for enseigne_slug in ENSEIGNES_ALLEMAGNE:
        for ville_slug in VILLES_ALLEMAGNE:
            try:
                produits = get_prospekt_allemagne(enseigne_slug, ville_slug)
                if produits:
                    print(f"  {enseigne_slug} à {ville_slug} : prospectus trouvé")
                tous_produits.extend(produits)
            except requests.HTTPError as e:
                print(f"  {enseigne_slug} à {ville_slug} : échec ({e})")
            time.sleep(DELAY_SECONDS)

    # Recoupement avec la liste de besoins (couvre aussi les produits
    # trouvés par le scraping par enseigne, pas seulement la recherche
    # ciblée ci-dessus)
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
