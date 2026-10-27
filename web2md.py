#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web2md.py : convertit une page Web en Markdown.

Fonctionnement :
	1. La page est téléchargée (--url), lue dans un fichier HTML local
	   (--input) ou sur l'entrée standard.
	2. Le contenu principal est repéré : sélecteur CSS fourni (--selector),
	   sinon balises sémantiques (<article>, <main>…), sinon analyse de la
	   densité de texte. Menus, pieds de page, publicités, commentaires,
	   boutons de partage, etc. sont retirés.
	3. Les blocs de code (<pre>) sont extraits à part, avec leur langage
	   (détecté d'après les classes CSS : language-xxx, highlight-source-xxx…),
	   puis remis tels quels dans des blocs « ``` » : l'indentation
	   (tabulations comprises) n'est jamais modifiée. Les numéros de ligne
	   et les boutons « Copier » sont supprimés.
	4. Images, vidéos, sons et contenus intégrés (iframe) sont conservés.
	   Les liens sont rendus absolus ou, avec --download-media, les fichiers
	   sont téléchargés et référencés par un chemin relatif.
	5. Le HTML nettoyé est converti en Markdown par « markdownify ».

Dépendances :
	pip install requests beautifulsoup4 markdownify lxml

Les messages d'état sont écrits sur la sortie d'erreur : le Markdown peut donc
être redirigé ou envoyé directement à md2dokuwiki.py.

Exemples :
	python3 web2md.py --url https://exemple.fr/article --output article.md
	python3 web2md.py --url https://exemple.fr/article --output article.md --download-media
	python3 web2md.py --input page.html --base-url https://exemple.fr/ --selector "div.post"
	python3 web2md.py --url https://exemple.fr/article | python3 md2dokuwiki.py > article.txt
"""

import argparse
import base64
import importlib.util
import mimetypes
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

try:
	import requests
	from bs4 import BeautifulSoup, NavigableString
	from markdownify import ATX, markdownify
except ImportError as error:
	print(
		f'❌ Module Python manquant : {error.name}\n'
		'   Installez les dépendances : pip install requests beautifulsoup4 markdownify lxml',
		file = sys.stderr,
	)
	sys.exit(1)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0'

# Analyseur HTML : lxml s'il est installé (plus rapide et plus tolérant)
HTML_PARSER = 'lxml' if importlib.util.find_spec('lxml') else 'html.parser'

# Un candidat « contenu principal » doit contenir au moins ce nombre de caractères
MIN_TEXT_LENGTH = 200

# Taille maximale d'un média téléchargé (les fichiers plus gros sont ignorés)
MAX_MEDIA_SIZE = 50 * 1024 * 1024

# Balises supprimées partout dans la page (aucun intérêt dans un Markdown).
# Remarque : <form> n'est volontairement pas listé, car certaines pages (ASP.NET)
# enveloppent tout leur contenu dans un formulaire.
JUNK_TAGS = [
	'script', 'style', 'noscript', 'template', 'svg', 'canvas',
	'nav', 'footer', 'aside', 'dialog', 'input', 'select', 'button',
]

# Candidats « contenu principal », par ordre de préférence
MAIN_SELECTORS = [
	'article',
	'main',
	'[role=main]',
	'#content',
	'#main-content',
	'.post-content',
	'.entry-content',
	'.article-content',
	'.article-body',
	'.post-body',
	'.markdown-body',
	'.content',
]

# Classes ou identifiants qui désignent des éléments « parasites » dans le contenu
# (le mot doit être entouré de « - », « _ », d'espaces ou du début/fin de chaîne)
NOISE_REGEX = re.compile(
	r'(?:^|[\s_-])(?:sidebar|share|sharing|social|related|newsletter|subscribe|'
	r'cookies?|consent|advert(?:isement)?s?|ads?|promo|breadcrumbs?|pagination|'
	r'comments?|popup|modal|skip-link|toc|table-of-contents)(?:$|[\s_-])',
	re.IGNORECASE,
)

# Symboles des ancres de titres (« # », « ¶ »…) : inutiles en Markdown
ANCHOR_SYMBOLS_REGEX = re.compile('[\\s\u200b#¶§🔗¤]+')

# --- Blocs de code ---------------------------------------------------------

# Jeton qui remplace temporairement chaque bloc de code pendant la conversion
# (uniquement des lettres et chiffres : markdownify n'y touche pas)
TOKEN_FORMAT = 'ZZCODEBLOCK{}ZZ'
TOKEN_REGEX = re.compile(r'^(?P<prefix>[^\n]*?)ZZCODEBLOCK(?P<index>\d+)ZZ[ \t]*$', re.MULTILINE)

# Éléments à supprimer à l'intérieur d'un <pre> (numéros de ligne, boutons…)
CODE_JUNK_SELECTOR = ', '.join([
	'button', '.copy', '.copy-button', '.copy-code-button', '.clipboard',
	'.line-numbers-rows', '.linenos', '.lineno', '.linenumber', '.line-number',
	'.lnt', '.ln', '.hljs-ln-numbers', '.hljs-ln-n', '.gutter',
])

# Conteneurs de ligne : un retour à la ligne est ajouté s'il manque
CODE_LINE_SELECTOR = 'span.line, div.line, div.code-line, span.code-line, div.ec-line, span.cl'

# Attributs HTML qui peuvent contenir le nom du langage
LANGUAGE_ATTRIBUTES = ['data-lang', 'data-language', 'data-code-language']

# Motifs de classes CSS indiquant le langage.
# Pour <pre> et <code> : language-xxx, lang-xxx, highlight-xxx, highlight-source-xxx
ELEMENT_LANGUAGE_PATTERNS = [
	re.compile(r'^highlight-source-([\w+#.-]+)$', re.IGNORECASE),
	re.compile(r'^(?:language|highlight)[-_]([\w+#.-]+)$', re.IGNORECASE),
	re.compile(r'^lang[-_]([\w+#.-]+)$', re.IGNORECASE),
]
# Pour les conteneurs parents, on reste plus prudent (pas de « lang-fr » de page)
WRAPPER_LANGUAGE_PATTERNS = ELEMENT_LANGUAGE_PATTERNS[:2]

# Valeurs qui ne désignent pas un vrai langage
NON_LANGUAGES = {
	'none', 'plain', 'plaintext', 'text', 'txt', 'nohighlight', 'hljs', 'auto',
	'source', 'block', 'wrapper', 'container', 'line', 'lines', 'default',
}

# Classes de Pandoc qui accompagnent le langage (<pre class="sourceCode python">)
SOURCE_CODE_CLASSES = {'sourcecode', 'numbersource', 'numberlines'}

# --- Médias ----------------------------------------------------------------

# Attributs utilisés par le chargement différé des images (lazy-loading)
LAZY_ATTRIBUTES = ['data-src', 'data-original', 'data-lazy-src', 'data-lazy', 'data-url']


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def log(message):
	"""Affiche un message d'état sur la sortie d'erreur."""
	print(message, file = sys.stderr)


def remove(element):
	"""Supprime un élément, sauf s'il a déjà été supprimé avec son parent."""
	if not element.decomposed:
		element.decompose()


def class_list(element):
	"""Retourne les classes CSS d'un élément sous forme de liste."""
	value = element.get('class') or []
	return [value] if isinstance(value, str) else list(value)


def text_length(element):
	"""Nombre de caractères de texte (espaces normalisés) d'un élément."""
	return len(element.get_text(' ', strip = True))


def link_density(element):
	"""Part du texte contenue dans des liens (0 = aucun lien, 1 = que des liens)."""
	total = text_length(element)
	if total == 0:
		return 1.0
	links = sum(text_length(link) for link in element.find_all('a'))
	return min(links / total, 1.0)


def describe_element(element):
	"""Décrit un élément de façon lisible : div#id.classe1.classe2."""
	label = element.name
	if element.get('id'):
		label += '#' + str(element['id'])
	classes = class_list(element)
	if classes:
		label += '.' + '.'.join(classes)
	return label


# ---------------------------------------------------------------------------
# Téléchargement des médias
# ---------------------------------------------------------------------------

class MediaDownloader:
	"""Télécharge les médias dans un dossier et renvoie leur chemin relatif."""

	def __init__(self, media_dir, link_base, user_agent, referer, timeout):
		self.media_dir = Path(media_dir)
		# Dossier par rapport auquel les chemins du Markdown sont calculés
		self.link_base = Path(link_base)
		self.timeout = timeout
		self.session = requests.Session()
		self.session.headers.update({'User-Agent': user_agent})
		# Certains sites refusent les images sans « Referer » (anti-hotlink)
		if referer:
			self.session.headers['Referer'] = referer
		self.cache = {}         # URL d'origine -> lien utilisé dans le Markdown
		self.used_names = set() # noms de fichiers déjà attribués
		self.count = 0          # nombre de fichiers téléchargés

	def fetch(self, url):
		"""Télécharge le média et renvoie le lien à utiliser (l'URL d'origine en cas d'échec)."""
		if url in self.cache:
			return self.cache[url]

		path = None
		try:
			if url.startswith('data:'):
				path = self.save_data_uri(url)
			elif urlparse(url).scheme in ('http', 'https'):
				path = self.download(url)
		except (OSError, ValueError, requests.RequestException) as error:
			log(f'⚠️  Média ignoré ({error}) : {url[:80]}')

		if path is None:
			link = url
		else:
			self.count += 1
			link = os.path.relpath(path, start = self.link_base).replace(os.sep, '/')
		self.cache[url] = link
		return link

	def reserve_path(self, url, content_type = '', default_stem = 'media'):
		"""Choisit un nom de fichier sûr et unique pour le média."""
		self.media_dir.mkdir(parents = True, exist_ok = True)

		# Nom d'après l'URL, réduit aux caractères sûrs
		name = unquote(Path(urlparse(url).path).name)
		name = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-.')[:100]
		stem, suffix = os.path.splitext(name)
		if not stem:
			stem = default_stem
		# Extension absente : on la déduit du type MIME
		if not suffix:
			suffix = mimetypes.guess_extension(content_type) or ''

		# Évite d'écraser un autre média portant le même nom
		candidate = stem + suffix
		counter = 2
		while candidate in self.used_names:
			candidate = f'{stem}-{counter}{suffix}'
			counter += 1
		self.used_names.add(candidate)
		return self.media_dir / candidate

	def download(self, url):
		"""Télécharge un fichier (en flux, avec limite de taille)."""
		path = None
		try:
			with self.session.get(url, stream = True, timeout = self.timeout) as response:
				response.raise_for_status()
				content_type = response.headers.get('Content-Type', '').split(';')[0].strip()
				if content_type == 'text/html':
					raise ValueError("la réponse n'est pas un média")
				if int(response.headers.get('Content-Length') or 0) > MAX_MEDIA_SIZE:
					raise ValueError('fichier trop volumineux')

				path = self.reserve_path(url, content_type)
				total = 0
				with open(path, 'wb') as target:
					for chunk in response.iter_content(chunk_size = 65536):
						total += len(chunk)
						if total > MAX_MEDIA_SIZE:
							raise ValueError('fichier trop volumineux')
						target.write(chunk)
		except Exception:
			# Ne laisse pas de fichier partiel derrière soi
			if path is not None and path.exists():
				path.unlink()
			raise
		log(f'🖼️  Téléchargé : {path.name}')
		return path

	def save_data_uri(self, url):
		"""Enregistre une image intégrée en base64 (data:image/png;base64,…)."""
		match = re.match(r'data:([\w/+.-]*)(;base64)?,(.*)$', url, re.DOTALL)
		if not match or not match.group(2):
			return None
		data = base64.b64decode(match.group(3))
		path = self.reserve_path('', match.group(1), default_stem = 'inline-image')
		path.write_bytes(data)
		return path


# ---------------------------------------------------------------------------
# Recherche du contenu principal
# ---------------------------------------------------------------------------

def remove_junk(soup):
	"""Supprime scripts, styles, menus, pieds de page, éléments cachés…"""
	for tag in soup.find_all(JUNK_TAGS):
		remove(tag)
	for tag in soup.select('[hidden], [style*="display:none"], [style*="display: none"]'):
		remove(tag)


def extract_title(soup):
	"""Titre de la page : premier <h1>, sinon og:title, sinon <title>."""
	heading = soup.find('h1')
	if heading and heading.get_text(strip = True):
		return ' '.join(heading.get_text().split())

	meta = soup.find('meta', attrs = {'property': 'og:title'})
	if meta and meta.get('content'):
		return ' '.join(meta['content'].split())

	if soup.title and soup.title.get_text(strip = True):
		return ' '.join(soup.title.get_text().split())
	return ''


def find_by_density(soup):
	"""Repère le bloc qui concentre le plus de texte « rédactionnel » (méthode type Readability)."""
	scores = {}
	nodes = {}

	def add(node, points):
		if node is None or node.name in ('html', '[document]'):
			return
		scores[id(node)] = scores.get(id(node), 0) + points
		nodes[id(node)] = node

	for paragraph in soup.find_all(['p', 'pre']):
		text = paragraph.get_text(' ', strip = True)
		if paragraph.name == 'p' and len(text) < 25:
			continue
		# Un point par paragraphe, plus un par virgule, plus un par tranche de 100 caractères
		points = 1 + text.count(',') + min(len(text) // 100, 3)
		if paragraph.name == 'pre':
			points += 5
		# Le parent reçoit tous les points, le grand-parent la moitié
		add(paragraph.parent, points)
		if paragraph.parent is not None:
			add(paragraph.parent.parent, points / 2)

	if not scores:
		return soup.body or soup

	# On pénalise les blocs qui sont surtout des liens (menus, listes d'articles)
	best = max(nodes.values(), key = lambda node: scores[id(node)] * (1 - link_density(node)))
	return best


def find_main_content(soup, selector):
	"""Retourne l'élément qui contient le contenu principal de la page."""
	# 1) Sélecteur imposé par l'utilisateur
	if selector:
		try:
			node = soup.select_one(selector)
		except Exception as error:
			raise ValueError(f'sélecteur CSS invalide « {selector} » : {error}')
		if node is None:
			raise ValueError(f'aucun élément ne correspond au sélecteur « {selector} »')
		return node

	# 2) Balises et classes usuelles : on garde le candidat le plus riche en texte
	for css in MAIN_SELECTORS:
		candidates = [node for node in soup.select(css) if text_length(node) >= MIN_TEXT_LENGTH]
		if candidates:
			return max(candidates, key = text_length)

	# 3) Dernier recours : analyse de la densité de texte
	return find_by_density(soup)


def remove_noise(root):
	"""Retire du contenu les blocs parasites reconnus à leur classe ou identifiant."""
	root_length = max(text_length(root), 1)

	for element in root.find_all(True):
		# Un parent supprimé a déjà emporté ses enfants
		if element.decomposed:
			continue
		# On ne touche jamais au code (ex. : <span class="comment"> d'un colorateur)
		if element.name in ('pre', 'code') or element.find_parent(['pre', 'code']):
			continue

		identifiers = ' '.join(class_list(element)) + ' ' + str(element.get('id') or '')
		if not NOISE_REGEX.search(identifiers):
			continue

		# Garde-fou : on ne supprime pas un conteneur qui renferme du code
		# ou la majeure partie du contenu (ex. : <div class="has-sidebar">)
		if element.find('pre') or text_length(element) > 0.5 * root_length:
			continue
		remove(element)


# ---------------------------------------------------------------------------
# Blocs de code
# ---------------------------------------------------------------------------

def language_from_classes(classes, patterns):
	"""Cherche un nom de langage dans une liste de classes CSS."""
	# SyntaxHighlighter : class="brush: python;"
	match = re.search(r'brush:\s*([\w+#.-]+)', ' '.join(classes))
	if match and match.group(1).lower() not in NON_LANGUAGES:
		return match.group(1).lower()

	for name in classes:
		for pattern in patterns:
			match = pattern.match(name)
			if match:
				language = match.group(1).lower()
				if language not in NON_LANGUAGES:
					return language
				break
	return ''


def detect_language(pre):
	"""Devine le langage d'un bloc de code (chaîne vide si inconnu)."""
	code = pre.find('code')
	targets = [pre] + ([code] if code else [])

	# 1) Attributs explicites (data-lang="python")
	for element in targets:
		for attribute in LANGUAGE_ATTRIBUTES:
			value = str(element.get(attribute) or '').strip().lower()
			if value and value not in NON_LANGUAGES:
				return value

	# 2) Classes du <pre> et du <code> (language-python, lang-python…)
	for element in targets:
		language = language_from_classes(class_list(element), ELEMENT_LANGUAGE_PATTERNS)
		if language:
			return language

	# 3) Pandoc : <pre class="sourceCode python">
	for element in targets:
		classes = class_list(element)
		if any(name.lower() == 'sourcecode' for name in classes):
			for name in classes:
				if name.lower() not in SOURCE_CODE_CLASSES and name.lower() not in NON_LANGUAGES:
					return name.lower()

	# 4) Conteneurs parents (GitHub : <div class="highlight-source-python">)
	ancestor = pre.parent
	for _ in range(3):
		if ancestor is None or ancestor.name in ('body', 'html', '[document]'):
			break
		language = language_from_classes(class_list(ancestor), WRAPPER_LANGUAGE_PATTERNS)
		if language:
			return language
		ancestor = ancestor.parent
	return ''


def extract_code_text(pre):
	"""Extrait le texte exact d'un bloc de code, sans numéros de ligne ni boutons."""
	for junk in pre.select(CODE_JUNK_SELECTOR):
		remove(junk)

	# <br> -> retour à la ligne
	for br in pre.find_all('br'):
		br.replace_with('\n')

	rows = pre.find_all('tr')
	if rows:
		# Colorateurs en tableau (highlight.js) : le code est dans la dernière cellule
		lines = []
		for row in rows:
			cells = row.find_all(['td', 'th'])
			lines.append(cells[-1].get_text() if cells else '')
		text = '\n'.join(lines)
	else:
		# Colorateurs qui enveloppent chaque ligne dans un <span>/<div> sans « \n »
		for line in pre.select(CODE_LINE_SELECTOR):
			sibling = line.next_sibling
			followed_by_newline = isinstance(sibling, NavigableString) and sibling.startswith('\n')
			if not line.get_text().endswith('\n') and not followed_by_newline:
				line.append('\n')
		text = pre.get_text()

	# Normalisation : espaces insécables et caractères de largeur nulle -> rien d'utile dans du code
	text = text.replace('\r\n', '\n').replace('\r', '\n')
	text = text.replace('\xa0', ' ').replace('\u200b', '')

	# On retire les espaces en fin de ligne et les lignes vides au début/à la fin,
	# mais jamais l'indentation (tabulations incluses)
	lines = [line.rstrip() for line in text.split('\n')]
	while lines and not lines[0]:
		lines.pop(0)
	while lines and not lines[-1]:
		lines.pop()
	return '\n'.join(lines)


def build_fence(code, language):
	"""Construit un bloc de code Markdown (barrière plus longue si le code contient « ``` »)."""
	longest = max((len(run) for run in re.findall(r'`{3,}', code)), default = 0)
	fence = '`' * max(3, longest + 1)
	return f'{fence}{language}\n{code}\n{fence}'


def flatten_highlight_tables(soup, root):
	"""Remplace les tableaux « numéros de ligne | code » (Pygments, Chroma) par leur seule colonne de code."""
	for table in root.select('table.highlighttable, table.lntable'):
		cells = table.find_all('td')
		if not cells:
			continue
		wrapper = soup.new_tag('div')
		for child in list(cells[-1].contents):
			wrapper.append(child)
		table.replace_with(wrapper)


def process_code_blocks(soup, root):
	"""Remplace chaque <pre> par un jeton et renvoie la liste des blocs Markdown."""
	blocks = []
	for pre in root.find_all('pre'):
		if pre.decomposed:
			continue
		code = extract_code_text(pre)
		if not code.strip():
			remove(pre)
			continue

		language = detect_language(pre)
		placeholder = soup.new_tag('p')
		placeholder.string = TOKEN_FORMAT.format(len(blocks))
		blocks.append(build_fence(code, language))
		pre.replace_with(placeholder)
	return blocks


def restore_code_blocks(markdown, blocks):
	"""Remet les blocs de code à la place des jetons, en respectant listes et citations."""

	def _replace(match):
		prefix = match.group('prefix')
		block = blocks[int(match.group('index'))]

		# Le jeton est précédé de texte (cas rare) : le bloc passe sur ses propres lignes
		if not re.fullmatch(r'[\s>]*(?:(?:[-*+]|\d+[.)])[ \t]+)*[\s>]*', prefix):
			return prefix.rstrip() + '\n\n' + block

		# Dans une liste ou une citation, chaque ligne du bloc reprend l'indentation
		# (les puces « - » deviennent des espaces, les « > » sont conservés)
		continuation = re.sub(r'[^\s>]', ' ', prefix)
		lines = block.split('\n')
		result = [prefix + lines[0]]
		for line in lines[1:]:
			result.append(continuation + line if line else continuation.rstrip())
		return '\n'.join(result)

	return TOKEN_REGEX.sub(_replace, markdown)


# ---------------------------------------------------------------------------
# Médias : images, vidéos, sons, contenus intégrés
# ---------------------------------------------------------------------------

def parse_srcset(value):
	"""Retourne la liste (url, taille) d'un attribut srcset."""
	candidates = []
	# Les candidats sont séparés par une virgule suivie d'une espace (ou d'un descripteur w/x)
	for part in re.split(r'(?<=[wx]),\s*|,\s+', value.strip()):
		tokens = part.split()
		if not tokens:
			continue
		try:
			size = float(tokens[1][:-1]) if len(tokens) > 1 else 0.0
		except ValueError:
			size = 0.0
		candidates.append((tokens[0], size))
	return candidates


def get_image_url(img):
	"""Trouve la vraie URL d'une image (lazy-loading, srcset, <picture>)."""
	# 1) Attributs de chargement différé : ils contiennent le vrai fichier
	for attribute in LAZY_ATTRIBUTES:
		value = str(img.get(attribute) or '').strip()
		if value:
			return value

	# 2) Attribut src classique (hors image intégrée, souvent un simple espace réservé)
	src = str(img.get('src') or '').strip()
	if src and not src.startswith('data:'):
		return src

	# 3) srcset de l'image, puis des <source> d'un <picture> : on prend la plus grande
	srcsets = [str(img.get(attribute) or '') for attribute in ('srcset', 'data-srcset')]
	picture = img.find_parent('picture')
	if picture:
		for source in picture.find_all('source'):
			srcsets.append(str(source.get('srcset') or source.get('data-srcset') or ''))
	for srcset in srcsets:
		candidates = parse_srcset(srcset)
		if candidates:
			return max(candidates, key = lambda candidate: candidate[1])[0]

	return src


def is_tracking_pixel(img):
	"""Détecte les images de 1 ou 2 pixels (pixels espions)."""
	try:
		return int(img.get('width', 99)) <= 2 and int(img.get('height', 99)) <= 2
	except (TypeError, ValueError):
		return False


def process_images(root, base_url, downloader):
	"""Rend les URL des images absolues (ou les télécharge)."""
	for img in root.find_all('img'):
		url = get_image_url(img)
		if not url or is_tracking_pixel(img):
			remove(img)
			continue

		if not url.startswith('data:'):
			url = urljoin(base_url, url)
		if downloader:
			url = downloader.fetch(url)
		img['src'] = url


def process_figure_captions(soup, root):
	"""Transforme chaque <figcaption> en paragraphe en italique."""
	for caption in root.find_all('figcaption'):
		text = ' '.join(caption.get_text().split())
		if not text:
			remove(caption)
			continue
		paragraph = soup.new_tag('p')
		emphasis = soup.new_tag('em')
		emphasis.string = text
		paragraph.append(emphasis)
		caption.replace_with(paragraph)


def process_links(root, base_url):
	"""Rend les liens absolus et retire les liens inutiles (ancres, javascript:…)."""
	for link in root.find_all('a'):
		href = str(link.get('href') or '').strip()
		text = link.get_text()
		has_media = link.find(['img', 'video', 'audio']) is not None
		in_heading = link.find_parent(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']) is not None

		# Ancre de titre (« # », « ¶ »…) : supprimée
		if in_heading and not has_media and not ANCHOR_SYMBOLS_REGEX.sub('', text):
			remove(link)
			continue
		# Lien vide, vers une ancre de la page ou en JavaScript : on garde seulement le texte
		if not href or href.startswith('#') or href.lower().startswith('javascript:'):
			link.unwrap()
			continue
		# Lien sans texte ni image (icône) : supprimé
		if not text.strip() and not has_media:
			remove(link)
			continue
		link['href'] = urljoin(base_url, href)


def process_embedded_media(soup, root, base_url, downloader):
	"""Remplace <video>, <audio> et <iframe> par des liens (Markdown n'a pas de balise équivalente)."""

	# Vidéos et sons : lien vers le fichier (+ image d'aperçu pour les vidéos)
	for tag_name, label in (('video', 'Vidéo'), ('audio', 'Audio')):
		for media in root.find_all(tag_name):
			source = str(media.get('src') or '')
			if not source:
				source_tag = media.find('source', src = True)
				source = str(source_tag['src']) if source_tag else ''
			poster = str(media.get('poster') or '')

			# L'aperçu et le lien sont dans deux paragraphes distincts
			wrapper = soup.new_tag('div')
			if poster:
				poster_url = urljoin(base_url, poster)
				if downloader:
					poster_url = downloader.fetch(poster_url)
				image_paragraph = soup.new_tag('p')
				image_paragraph.append(soup.new_tag('img', src = poster_url, alt = f'Aperçu : {label.lower()}'))
				wrapper.append(image_paragraph)
			if source:
				absolute = urljoin(base_url, source)
				name = Path(unquote(urlparse(absolute).path)).name or label.lower()
				link = soup.new_tag('a', href = downloader.fetch(absolute) if downloader else absolute)
				link.string = f'{label} : {name}'
				link_paragraph = soup.new_tag('p')
				link_paragraph.append(link)
				wrapper.append(link_paragraph)

			if wrapper.contents:
				media.replace_with(wrapper)
			else:
				remove(media)

	# Contenus intégrés (YouTube, Vimeo, cartes…) : lien vers la source
	for frame in root.find_all('iframe'):
		source = str(frame.get('src') or frame.get('data-src') or '').strip()
		if not source or source.lower().startswith(('about:', 'javascript:')):
			remove(frame)
			continue
		absolute = urljoin(base_url, source)
		title = ' '.join(str(frame.get('title') or '').split()) or urlparse(absolute).netloc
		paragraph = soup.new_tag('p')
		link = soup.new_tag('a', href = absolute)
		link.string = f'Contenu intégré : {title}'
		paragraph.append(link)
		frame.replace_with(paragraph)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def clean_markdown(markdown):
	"""Supprime les lignes vides en trop."""
	markdown = re.sub(r'\n[ \t]+\n', '\n\n', markdown)
	markdown = re.sub(r'\n{3,}', '\n\n', markdown)
	return markdown.strip()


def convert_page(content, base_url, selector, downloader, add_source):
	"""Convertit le HTML d'une page en Markdown."""
	soup = BeautifulSoup(content, HTML_PARSER)

	# Nettoyage global, titre, puis repérage du contenu principal
	remove_junk(soup)
	page_title = extract_title(soup)
	root = find_main_content(soup, selector)
	log(f'📄 Contenu principal : {describe_element(root)}')
	remove_noise(root)

	# Blocs de code en premier : ils sont mis de côté, à l'abri de toute modification
	flatten_highlight_tables(soup, root)
	blocks = process_code_blocks(soup, root)

	# Médias et liens (les liens avant les vidéos, car ces dernières créent leurs propres liens)
	process_images(root, base_url, downloader)
	process_figure_captions(soup, root)
	process_links(root, base_url)
	process_embedded_media(soup, root, base_url, downloader)

	# HTML -> Markdown, puis retour des blocs de code
	markdown = markdownify(str(root), heading_style = ATX, bullets = '-')
	markdown = clean_markdown(markdown)
	markdown = restore_code_blocks(markdown, blocks)
	log(f'🧩 {len(blocks)} bloc(s) de code')

	# Le titre de la page est ajouté s'il n'est pas déjà dans le contenu retenu
	if page_title and root.find('h1') is None:
		markdown = f'# {page_title}\n\n{markdown}'
	if add_source and base_url:
		markdown += f'\n\n---\n\nSource : <{base_url}>'
	return markdown + '\n'


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def parse_arguments():
	"""Définit et lit les options de la ligne de commande."""
	parser = argparse.ArgumentParser(
		description = 'Convertit une page Web en Markdown '
		              '(contenu principal, médias et blocs de code).',
	)
	parser.add_argument(
		'--url',
		metavar = 'URL',
		help = 'adresse de la page à convertir',
	)
	parser.add_argument(
		'--input',
		metavar = 'FICHIER',
		help = 'fichier HTML local à convertir (« - » ou rien : entrée standard)',
	)
	parser.add_argument(
		'--base-url',
		metavar = 'URL',
		default = '',
		help = 'adresse d\'origine de la page, pour rendre les liens relatifs absolus '
		       '(utile avec --input)',
	)
	parser.add_argument(
		'--output',
		metavar = 'FICHIER',
		help = 'fichier Markdown à écrire (sortie standard par défaut)',
	)
	parser.add_argument(
		'--selector',
		metavar = 'CSS',
		help = 'sélecteur CSS du contenu principal, si la détection automatique se trompe '
		       '(ex. : "div.post-body")',
	)
	parser.add_argument(
		'--download-media',
		action = 'store_true',
		help = 'télécharge les images (et aperçus de vidéos) et les référence par un chemin relatif',
	)
	parser.add_argument(
		'--media-dir',
		metavar = 'DOSSIER',
		help = 'dossier des médias téléchargés (active --download-media ; '
		       'par défaut : <sortie>_media, ou « media » sans --output)',
	)
	parser.add_argument(
		'--add-source',
		action = 'store_true',
		help = "ajoute l'adresse de la page en fin de document",
	)
	parser.add_argument(
		'--timeout',
		metavar = 'SECONDES',
		type = int,
		default = 30,
		help = 'délai maximal de chaque requête réseau (par défaut : 30)',
	)
	parser.add_argument(
		'--user-agent',
		metavar = 'TEXTE',
		default = DEFAULT_USER_AGENT,
		help = "identifiant du navigateur envoyé aux sites (par défaut : Firefox)",
	)
	return parser.parse_args()


def load_html(args):
	"""Retourne le contenu HTML (octets) et l'adresse de base de la page."""
	if args.url:
		log(f'⏳ Téléchargement de {args.url}…')
		response = requests.get(
			args.url,
			headers = {'User-Agent': args.user_agent},
			timeout = args.timeout,
		)
		response.raise_for_status()
		# response.url tient compte des redirections éventuelles
		return response.content, response.url

	if args.input and args.input != '-':
		with open(args.input, 'rb') as source:
			return source.read(), args.base_url

	return sys.stdin.buffer.read(), args.base_url


def main():
	args = parse_arguments()

	# Force l'UTF-8 sur les flux standard (utile surtout sous Windows)
	for stream in (sys.stdin, sys.stdout, sys.stderr):
		stream.reconfigure(encoding = 'utf-8')

	if not args.url and not args.input and sys.stdin.isatty():
		log("❌ Indiquez une page avec --url, un fichier avec --input, "
		    "ou envoyez du HTML sur l'entrée standard.")
		return 1

	# Lecture de la page
	try:
		content, base_url = load_html(args)
	except requests.RequestException as error:
		log(f'❌ Impossible de télécharger la page : {error}')
		return 1
	except OSError as error:
		log(f'❌ Impossible de lire le fichier : {error}')
		return 1

	# Préparation du téléchargement des médias
	downloader = None
	media_dir = None
	if args.download_media or args.media_dir:
		output_path = Path(args.output) if args.output else None
		if args.media_dir:
			media_dir = Path(args.media_dir)
		elif output_path:
			media_dir = output_path.with_name(output_path.stem + '_media')
		else:
			media_dir = Path('media')
		# Les chemins du Markdown sont relatifs au dossier du fichier de sortie
		link_base = output_path.resolve().parent if output_path else Path.cwd()
		referer = base_url if base_url.startswith('http') else ''
		downloader = MediaDownloader(media_dir, link_base, args.user_agent, referer, args.timeout)

	# Conversion
	try:
		markdown = convert_page(content, base_url, args.selector, downloader, args.add_source)
	except ValueError as error:
		log(f'❌ {error}')
		return 1

	if downloader and downloader.count:
		log(f'🖼️  {downloader.count} média(s) enregistré(s) dans {media_dir}')

	# Écriture du résultat (fins de ligne « \n » quel que soit le système)
	if args.output:
		try:
			with open(args.output, 'w', encoding = 'utf-8', newline = '\n') as target:
				target.write(markdown)
		except OSError as error:
			log(f"❌ Impossible d'écrire le fichier : {error}")
			return 1
		log(f'✅ Conversion terminée → {args.output}')
	else:
		sys.stdout.write(markdown)
		log('✅ Conversion terminée')

	return 0


if __name__ == '__main__':
	sys.exit(main())
