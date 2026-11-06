"""law_code -> SR number, from aliases.json (Fedlex official abbreviations). Our own table.
Keys are upper-cased with spaces and dots removed, as the dataset writes codes. A code is looked up first among
the abbreviations of the decision's language, then of all languages; a code two acts share goes to the act that
uses it in that language, else in German, else the shorter SR number (the main code over an ordinance).
A leading lowercase 'a' (aCP, aStGB) marks the former edition of the same act: the version in force on the
decision day is what the overlay pins anyway."""
import json, re
from pathlib import Path
# Former titles of acts still in force under the same SR number, which Fedlex does not list as abbreviations.
# Abbreviations cantons commonly use for their own laws: in a cantonal decision they mean the canton's law, so they
# are not linked to the federal act of the same abbreviation. LPA and KV are left out everywhere (Geneva's procedure
# law and the cantonal constitutions are cited far more than the federal acts), as is any cantonal constitution
# filed in the SR (131.*).
CANTONAL_HOMONYMS = {'STG', 'KV', 'PBG', 'VG', 'PG', 'GVG', 'OG', 'LPA', 'LC', 'GG', 'LEX', 'POG', 'SG'}
NEVER = {'LPA', 'KV'}
FORMER = {'AUG': '142.20', 'LETR': '142.20', 'LSTR': '142.20', 'ANAG': '142.20', 'AIMP': '351.1'}
def key(s): return re.sub(r'[\s.]', '', s).upper()
class Resolver:
    def __init__(self, path=Path(__file__).with_name('aliases.json')):
        a = json.load(open(path, encoding='utf-8'))
        self.by = {}  # lang -> key -> set(sr)
        for sr, v in a.items():
            for lang, names in v['short'].items():
                for n in names:
                    self.by.setdefault(lang, {}).setdefault(key(n), set()).add(sr)
        self.cache = {}
    def pick(self, srs, k):
        if len(srs) == 1: return next(iter(srs))
        de = [s for s in srs if s in self.by.get('de', {}).get(k, ())]
        pool = de or list(srs)
        return sorted(pool, key=lambda s: (len(s), s))[0]
    def __call__(self, code, lang=None, cantonal=False):
        ck = (code, lang, cantonal)
        if ck in self.cache: return self.cache[ck]
        raw = code or ''
        if len(raw) > 2 and raw[0] == 'a' and raw[1].isupper(): raw = raw[1:]
        k = key(raw)
        if k in NEVER or (cantonal and k in CANTONAL_HOMONYMS):
            self.cache[ck] = None
            return None
        sr = FORMER.get(k)
        if not sr:
            for l in ([lang] if lang else []) + ['de', 'fr', 'it', 'rm', 'en']:
                srs = self.by.get(l, {}).get(k)
                if srs: sr = self.pick(srs, k); break
        if sr and sr.startswith('131.'):
            sr = None
        self.cache[ck] = sr
        return sr
