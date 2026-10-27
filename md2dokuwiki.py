#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
md2dokuwiki.py : convertit un document Markdown en syntaxe DokuWiki.

Fonctionnement :
	1. Pandoc fait la conversion Markdown -> DokuWiki. Un petit filtre Lua
	   (embarqué dans ce script) remplace les blocs de code par des blocs
	   du plugin CodePrism : <codeprism lang=python> ... </codeprism>.
	2. Le texte obtenu est ensuite « typographié » à la française :
	   - espaces insécables avant « : ; ! ? » et à l'intérieur des guillemets ;
	   - guillemets droits ou courbes remplacés par des guillemets français ;
	   - apostrophes courbes remplacées par des apostrophes droites.
	   Les zones sensibles (code, liens, URL…) sont protégées pendant ce
	   traitement afin de ne jamais être modifiées.

Exemples :
	python3 md2dokuwiki.py --input page.md --output page.txt
	python3 md2dokuwiki.py --input page.md > page.txt
	cat page.md | python3 md2dokuwiki.py > page.txt
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Espace insécable (U+00A0). Elle est écrite telle quelle dans le fichier de
# sortie : DokuWiki la conserve et le navigateur l'affiche sans coupure.
NBSP = '\u00a0'

# Délimiteurs des « jetons » qui remplacent temporairement les zones
# protégées (caractères de la zone d'usage privé d'Unicode, jamais utilisés
# dans un texte normal).
TOKEN_START = '\ue000'
TOKEN_END = '\ue001'

# Filtre Pandoc (Lua) : transforme chaque bloc de code en bloc CodePrism.
# Il est écrit dans un fichier temporaire au moment de la conversion.
LUA_FILTER = r'''
-- Correspondance entre les noms de langages Markdown et ceux de Prism.js
-- (à compléter selon vos besoins).
local ALIASES = {
	delphi = "pascal",
	dpr = "pascal",
	pas = "pascal",
	objectpascal = "pascal",
}

-- Attributs CodePrism que l'on peut transmettre depuis le Markdown, par ex. :
--   ```{.python title=hello.py hl=2-4}
local PASSTHROUGH = { "title", "el", "sl", "hl", "cmd", "cmdout", "css", "user" }

function CodeBlock(block)
	local tag = "<codeprism"

	-- Le premier « classe » du bloc est le langage (```python)
	local lang = block.classes[1]
	if lang and lang ~= "numberLines" then
		lang = ALIASES[lang:lower()] or lang
		tag = tag .. " lang=" .. lang
	end

	-- Numérotation des lignes : ```{.python .numberLines startFrom="10"}
	for _, class in ipairs(block.classes) do
		if class == "numberLines" then
			tag = tag .. " el=true"
		end
	end
	if block.attributes["startFrom"] then
		tag = tag .. " sl=" .. block.attributes["startFrom"]
	end

	-- Autres attributs CodePrism transmis tels quels
	for _, key in ipairs(PASSTHROUGH) do
		local value = block.attributes[key]
		if value then
			tag = tag .. " " .. key .. "=" .. value
		end
	end

	-- Bloc brut : Pandoc l'écrit tel quel dans la sortie DokuWiki
	return pandoc.RawBlock("dokuwiki", tag .. ">\n" .. block.text .. "\n</codeprism>\n")
end
'''

# Zones à ne jamais modifier lors de la typographie (appliquées dans l'ordre).
# Chaque entrée : (expression régulière, drapeaux).
PROTECTED_PATTERNS = [
	# Blocs de code (CodePrism, natifs DokuWiki), HTML/PHP brut et nowiki
	(r'<(codeprism|code|file|html|php|nowiki)\b[^>]*>.*?</\1>', re.DOTALL | re.IGNORECASE),
	# Texte désactivé : %%...%%
	(r'%%.*?%%', re.DOTALL),
	# Code en ligne : ''code''
	(r"''.+?''", 0),
	# Cible des liens : [[cible|libellé]] (on ne protège que la cible)
	(r'(?<=\[\[)[^\]|]+', 0),
	# Cible des images : {{cible|légende}} (idem)
	(r'(?<=\{\{)[^}|]+', 0),
	# Adresses web écrites telles quelles dans le texte
	(r'(?:https?|ftp)://[^\s\ue000\ue001]+', 0),
	# Entités HTML (&amp; etc.) : leur « ; » ne doit pas être touché
	(r'&#?\w+;', 0),
]


# ---------------------------------------------------------------------------
# Protection / restauration des zones sensibles
# ---------------------------------------------------------------------------

def protect(text, pattern, store, flags):
	"""Remplace chaque occurrence du motif par un jeton et la mémorise."""

	def _replace(match):
		# On garde le texte original et on renvoie un jeton numéroté
		store.append(match.group(0))
		return TOKEN_START + str(len(store) - 1) + TOKEN_END

	return re.sub(pattern, _replace, text, flags = flags)


def restore(text, store):
	"""Remet en place les textes d'origine à la place des jetons."""
	token = re.compile(TOKEN_START + r'(\d+)' + TOKEN_END)

	# Boucle : un fragment protégé peut lui-même contenir un jeton
	while token.search(text):
		text = token.sub(lambda match: store[int(match.group(1))], text)
	return text


# ---------------------------------------------------------------------------
# Typographie française
# ---------------------------------------------------------------------------

def apply_french_typography(text, add_missing_spaces):
	"""Applique les règles typographiques françaises à un texte (déjà protégé)."""

	# 1) Apostrophes courbes -> apostrophes droites
	text = re.sub('[\u2019\u2018\u02bc]', "'", text)

	# 2) Guillemets anglais courbes -> guillemets français
	text = re.sub('\u201c[ \t]*', '«' + NBSP, text)
	text = re.sub('[ \t]*\u201d', NBSP + '»', text)

	# 3) Paires de guillemets droits (sur une même ligne) -> guillemets français
	text = re.sub(
		r'"[ \t]*([^"\n]+?)[ \t]*"',
		lambda match: '«' + NBSP + match.group(1) + NBSP + '»',
		text,
	)

	# 4) Espaces insécables à l'intérieur des guillemets français
	#    (existants ou saisis directement dans le Markdown)
	text = re.sub('«[ \t\u00a0\u202f]*', '«' + NBSP, text)
	text = re.sub('(?<=\\S)[ \t\u00a0\u202f]*»', NBSP + '»', text)

	# 5) Espace (même normale) avant « : ; ! ? » -> espace insécable.
	#    Le (?<=\S) évite de toucher à l'indentation des listes DokuWiki.
	text = re.sub(
		r'(?<=\S)[ \t\u202f]+([:;!?])',
		lambda match: NBSP + match.group(1),
		text,
	)

	# 6) (Facultatif) Ajoute l'espace insécable manquante : « Bonjour! » -> « Bonjour ! ».
	#    Heuristique prudente : le signe doit suivre un mot (ou « ) ] ») et être
	#    suivi d'une espace, d'une fin de ligne ou d'un autre signe. Ainsi
	#    « 12:30 », « ns:page » ou « std::vector » ne sont pas modifiés.
	if add_missing_spaces:
		text = re.sub(
			r'(?<=[\w)\]»' + TOKEN_END + r'])([:;!?])(?=[\s*_!?;:\u00a0]|$)',
			lambda match: NBSP + match.group(1),
			text,
			flags = re.MULTILINE,
		)

	return text


def typographize(dokuwiki_text, add_missing_spaces):
	"""Protège les zones sensibles, applique la typographie, puis restaure."""
	store = []
	protected = dokuwiki_text

	for pattern, flags in PROTECTED_PATTERNS:
		protected = protect(protected, pattern, store, flags)

	protected = apply_french_typography(protected, add_missing_spaces)
	return restore(protected, store)


# ---------------------------------------------------------------------------
# Appel de Pandoc
# ---------------------------------------------------------------------------

def run_pandoc(markdown, pandoc_path):
	"""Convertit le Markdown en DokuWiki avec Pandoc et le filtre Lua."""

	# Le filtre Lua doit exister sous forme de fichier : on le crée
	# dans un dossier temporaire supprimé automatiquement.
	with tempfile.TemporaryDirectory() as temp_dir:
		filter_path = Path(temp_dir) / 'codeprism.lua'
		filter_path.write_text(LUA_FILTER, encoding = 'utf-8')

		command = [
			pandoc_path,
			# « -smart » : on garde les guillemets/apostrophes droits tels quels,
			# c'est ce script qui s'occupe de la typographie.
			# « +autolink_bare_uris » : les URL nues deviennent de vrais liens.
			'--from', 'markdown-smart+autolink_bare_uris',
			'--to', 'dokuwiki',
			# Conserve les tabulations (sinon Pandoc les remplace par des espaces,
			# ce qui altère le code des blocs)
			'--preserve-tabs',
			# Pas de retour à la ligne automatique dans les paragraphes
			'--wrap', 'none',
			'--lua-filter', str(filter_path),
		]

		result = subprocess.run(
			command,
			input = markdown,
			capture_output = True,
			text = True,
			encoding = 'utf-8',
			check = False,
		)

	if result.returncode != 0:
		raise RuntimeError(result.stderr.strip() or 'erreur inconnue de Pandoc')
	return result.stdout


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def parse_arguments():
	"""Définit et lit les options de la ligne de commande."""
	parser = argparse.ArgumentParser(
		description = 'Convertit un document Markdown en DokuWiki '
		              '(code via CodePrism, typographie française).',
	)
	parser.add_argument(
		'--input',
		metavar = 'FICHIER',
		help = 'fichier Markdown à convertir (entrée standard par défaut)',
	)
	parser.add_argument(
		'--output',
		metavar = 'FICHIER',
		help = 'fichier DokuWiki à écrire (sortie standard par défaut)',
	)
	parser.add_argument(
		'--pandoc',
		metavar = 'CHEMIN',
		default = 'pandoc',
		help = 'commande ou chemin de Pandoc (par défaut : pandoc)',
	)
	parser.add_argument(
		'--skip-typography',
		action = 'store_true',
		help = 'ne pas appliquer les règles de typographie française',
	)
	parser.add_argument(
		'--no-add-missing-spaces',
		action = 'store_true',
		help = "ne pas ajouter l'espace insécable manquante avant « : ; ! ? »",
	)
	return parser.parse_args()


def main():
	args = parse_arguments()

	# Force l'UTF-8 sur les flux standard (utile surtout sous Windows)
	for stream in (sys.stdin, sys.stdout, sys.stderr):
		stream.reconfigure(encoding = 'utf-8')

	# Vérifie que Pandoc est bien disponible
	if shutil.which(args.pandoc) is None:
		print(
			'❌ Pandoc est introuvable. Installez-le : https://pandoc.org/installing.html',
			file = sys.stderr,
		)
		return 1

	# Lecture du Markdown
	try:
		if args.input:
			with open(args.input, 'r', encoding = 'utf-8') as source:
				markdown = source.read()
		else:
			markdown = sys.stdin.read()
	except OSError as error:
		print(f'❌ Impossible de lire le fichier : {error}', file = sys.stderr)
		return 1

	print('⏳ Conversion en cours…', file = sys.stderr)

	# Conversion Pandoc, puis typographie
	try:
		dokuwiki = run_pandoc(markdown, args.pandoc)
	except RuntimeError as error:
		print(f'❌ Pandoc a échoué : {error}', file = sys.stderr)
		return 1

	if not args.skip_typography:
		dokuwiki = typographize(
			dokuwiki,
			add_missing_spaces = not args.no_add_missing_spaces,
		)

	# Écriture du résultat (fins de ligne « \n » quel que soit le système)
	if args.output:
		try:
			with open(args.output, 'w', encoding = 'utf-8', newline = '\n') as target:
				target.write(dokuwiki)
		except OSError as error:
			print(f'❌ Impossible d\'écrire le fichier : {error}', file = sys.stderr)
			return 1
		print(f'✅ Conversion terminée → {args.output}', file = sys.stderr)
	else:
		sys.stdout.write(dokuwiki)
		print('✅ Conversion terminée', file = sys.stderr)

	return 0


if __name__ == '__main__':
	sys.exit(main())
