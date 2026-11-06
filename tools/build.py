"""Build one Swiss case-law repo (federal or one canton) from the pinned Parquet cache, straight into git fast-import.

  python build.py <group> <cache dir> <out repo dir> --today YYYY-MM-DD [--law-repo ~/Documents/swiss-law]

group: 'federal' or a canton code (ag, ai, ... zh). One commit per decision (Kind: court), dated the day it was
decided; then one Lexifina documentation commit with README, REPRODUCE.md, corpus.json and the judgment overlay
judgments/switzerland/bund/acts/<SR>/art-NNN.jsonl. Deterministic: the same cache gives the same hashes."""
from __future__ import annotations

import argparse
import bisect
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from resolve import Resolver  # noqa: E402

CANTONS = {'ag': 'aargau', 'ai': 'appenzell-innerrhoden', 'ar': 'appenzell-ausserrhoden', 'be': 'bern',
           'bl': 'basel-landschaft', 'bs': 'basel-stadt', 'fr': 'fribourg', 'ge': 'geneva', 'gl': 'glarus',
           'gr': 'graubunden', 'ju': 'jura', 'lu': 'lucerne', 'ne': 'neuchatel', 'nw': 'nidwalden', 'ow': 'obwalden',
           'sg': 'st-gallen', 'sh': 'schaffhausen', 'so': 'solothurn', 'sz': 'schwyz', 'tg': 'thurgau', 'ti': 'ticino',
           'ur': 'uri', 'vd': 'vaud', 'vs': 'valais', 'zg': 'zug', 'zh': 'zurich'}
CANTON_NAMES = {'ag': 'Aargau', 'ai': 'Appenzell Innerrhoden', 'ar': 'Appenzell Ausserrhoden', 'be': 'Bern',
                'bl': 'Basel-Landschaft', 'bs': 'Basel-Stadt', 'fr': 'Fribourg', 'ge': 'Geneva', 'gl': 'Glarus',
                'gr': 'Graubünden', 'ju': 'Jura', 'lu': 'Lucerne', 'ne': 'Neuchâtel', 'nw': 'Nidwalden',
                'ow': 'Obwalden', 'sg': 'St. Gallen', 'sh': 'Schaffhausen', 'so': 'Solothurn', 'sz': 'Schwyz',
                'tg': 'Thurgau', 'ti': 'Ticino', 'ur': 'Uri', 'vd': 'Vaud', 'vs': 'Valais', 'zg': 'Zug', 'zh': 'Zurich'}
LANG = {'ge': 'fr', 'vd': 'fr', 'ne': 'fr', 'ju': 'fr', 'fr': 'fr', 'vs': 'fr', 'ti': 'it'}
FEDERAL_COURTS = {'bger': 'Bundesgericht', 'bge': 'Bundesgericht (BGE)', 'bvger': 'Bundesverwaltungsgericht',
                  'bstger': 'Bundesstrafgericht', 'bpatger': 'Bundespatentgericht', 'mkg': 'Militärkassationsgericht',
                  'finma': 'FINMA', 'finma_versicherungsrecht': 'FINMA (Versicherungsrecht)', 'weko': 'WEKO',
                  'edoeb': 'EDÖB', 'comcom': 'ComCom', 'elcom': 'ElCom', 'postcom': 'PostCom', 'ubi': 'UBI',
                  'estv': 'ESTV', 'bazg': 'BAZG', 'esbk': 'ESBK', 'eschk': 'ESchK', 'emark': 'EMARK',
                  'rab': 'RAB', 'sav_international': 'SAV (international)', 'sav_kantone': 'SAV (Kantone)',
                  'ta_sst': 'Tribunal arbitral du sport (SST)', 'preisueberwacher': 'Preisüberwacher',
                  'ch_bundesrat': 'Bundesrat'}
SKIP_FILES = ('ch_vb',)
LAW_CORPUS = 'switzerland/bund/acts'
ZURICH = timezone(timedelta(hours=1))
EPOCH_NOON = 11 * 3600  # 1970-01-01 12:00 Zurich (UTC+1)
COMMITTER = b'Lexifina <corpus@lexifina.com>'
ART = re.compile(r'^\s*(\d+)\s*([a-z]*)\s*$', re.I)
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def files_for(cache: Path, group: str) -> list[str]:
    out = []
    for f in sorted(glob.glob(str(cache / 'data' / '*.parquet'))):
        n = Path(f).stem
        if n.startswith(SKIP_FILES) or re.search(r'\d', n.split('_')[0]) or n.startswith('delta'):
            continue
        prefix = n.split('_')[0]
        if (group == 'federal' and prefix not in CANTONS) or prefix == group:
            out.append(f)
    return out


def court_name(court: str) -> str:
    if court in FEDERAL_COURTS:
        return FEDERAL_COURTS[court]
    p, _, rest = court.partition('_')
    return f"{p.upper()} {rest.replace('_', ' ').title()}".strip() if p in CANTONS else court


def slug(s: str) -> str:
    return re.sub(r'-{2,}', '-', re.sub(r'[^A-Za-z0-9._-]+', '-', s)).strip('-.')[:120] or 'x'


def email_local(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:60] or 'decision'


def art_file(article: str | None) -> str | None:
    m = ART.match(article or '')
    return f'art-{int(m.group(1)):03d}{m.group(2).lower()}.md' if m else None


def law_index(law_repo: Path):
    """(sorted in-force days, shas) of switzerland-law main, and every article path it has ever held."""
    out = subprocess.run(['git', '-C', str(law_repo), 'log', 'origin/main', '--first-parent', '--format=%H%x09%(trailers:key=In-force,key=Consolidated,valueonly,separator=%x2C)'],
                         capture_output=True, text=True, encoding='utf-8', check=True).stdout
    pairs = []
    for line in out.splitlines():
        sha, _, d = line.partition('\t')
        d = next((x.strip()[:10] for x in d.split(',') if ISO.match(x.strip()[:10])), None)
        if d:
            pairs.append((d, sha))
    pairs.sort()
    paths = set(subprocess.run(['git', '-C', str(law_repo), 'log', 'origin/main', '--format=', '--name-only'],
                               capture_output=True, text=True, encoding='utf-8', check=True).stdout.split())
    return [p[0] for p in pairs], [p[1] for p in pairs], paths


def yaml_str(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('group'), ap.add_argument('cache'), ap.add_argument('out')
    ap.add_argument('--today', required=True)
    ap.add_argument('--law-repo', default=str(Path.home() / 'Documents' / 'swiss-law'))
    ap.add_argument('--chunk-bytes', type=int, default=2_500_000_000, help='raw text bytes between push checkpoints')
    a = ap.parse_args()
    cache, out, group = Path(a.cache), Path(a.out), a.group
    rev = (cache / 'REVISION').read_text().strip()
    files = files_for(cache, group)
    if not files:
        raise SystemExit(f'no files for {group}')
    lang = LANG.get(group, 'de')
    resolver = Resolver()
    days, shas, law_paths = law_index(Path(a.law_repo))
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET memory_limit='{os.environ.get('CASELAW_DUCKDB_MEM', '6GB')}'")
    tmp = Path(a.out).parent / '.duckdb-tmp' / group
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp.as_posix()}'")
    con.execute("SET threads=4")
    src = f"read_parquet({[f.replace(chr(92), '/') for f in files]!r}, union_by_name=true)"
    cols = ['decision_id', 'court', 'chamber', 'docket_number', 'docket_number_2', 'decision_date', 'publication_date',
            'language', 'title', 'legal_area', 'regeste', 'full_text', 'source_url', 'pdf_url', 'bge_reference',
            'cited_decisions', 'judges', 'clerks', 'content_hash', 'decision_type', 'proceeding_type']
    have = {r[0] for r in con.execute(f"describe select * from {src}").fetchall()}
    sel = ', '.join(c if c in have else f'NULL as {c}' for c in cols)
    con.execute(f"create temp table d as select {sel}, coalesce(decision_date, publication_date) as day from {src}")
    refs_src = str(cache / 'graph' / 'statute_references.parquet').replace('\\', '/')
    con.execute(f"create temp table r as select r.decision_id, law_code, article, paragraph, mention_count from '{refs_src}' r semi join d using (decision_id)")
    n_dec = con.execute('select count(*) from d').fetchone()[0]
    print(f'{group}: {len(files)} files, {n_dec} decisions', file=sys.stderr, flush=True)

    # cites per decision (resolved to SR files) for trailers and the overlay
    refs: dict[str, list] = {}
    for did, code, art, par, n in con.execute('select decision_id, law_code, article, paragraph, mention_count from r order by decision_id, law_code, article, paragraph').fetchall():
        refs.setdefault(did, []).append((code, art, par, n))

    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'{out} is not empty')
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(out)], check=True)
    subprocess.run(['git', '-C', str(out), 'config', 'core.autocrlf', 'false'], check=True)
    fi = subprocess.Popen(['git', '-C', str(out), 'fast-import', '--quiet', '--done', f'--export-marks={out / ".git" / "marks"}',
                          '--big-file-threshold=64m'], stdin=subprocess.PIPE)
    w = fi.stdin.write

    overlay: dict[str, list] = {}
    unresolved: dict[tuple, int] = {}
    used_paths: set[str] = set()
    mark = 0
    raw_bytes = 0
    checkpoints = []
    clamp = 0
    first_day = last_day = None
    undated = []
    cur = con.execute("select * from d order by day nulls last, court, docket_number, decision_id")
    names = [x[0] for x in cur.description]
    while True:
        batch = cur.fetchmany(2000)
        if not batch:
            break
        for row in batch:
            r = dict(zip(names, row))
            day = r['day'] if r['day'] and ISO.match(r['day']) else None
            if not day:
                undated.append(r)
                continue
            first_day = first_day or day
            last_day = day
            court = r['court'] or group
            docket = r['docket_number'] or r['bge_reference'] or r['decision_id']
            year = day[:4]
            base = f"decisions/{slug(court)}/{year}/{slug(docket)}"
            path = base + '.md'
            if path in used_paths:
                path = f"{base}--{hashlib.sha1(r['decision_id'].encode()).hexdigest()[:8]}.md"
            used_paths.add(path)
            cname = court_name(court)
            fm = {'decision_id': r['decision_id'], 'court': cname, 'court_id': court, 'chamber': r['chamber'],
                  'docket': r['docket_number'], 'docket_2': r['docket_number_2'], 'bge_reference': r['bge_reference'],
                  'decided': r['decision_date'], 'published': r['publication_date'], 'language': r['language'],
                  'legal_area': r['legal_area'], 'decision_type': r['decision_type'], 'judges': r['judges'],
                  'clerks': r['clerks'], 'source_url': r['source_url'], 'pdf_url': r['pdf_url']}
            cited = []
            try:
                cited = json.loads(r['cited_decisions']) if r['cited_decisions'] else []
            except (ValueError, TypeError):
                pass
            cites_law = []
            for code, art, par, n in refs.get(r['decision_id'], ()):
                sr = resolver(code, r['language'] or lang, cantonal=group != 'federal')
                af = art_file(art)
                p = f'{sr}/{af}' if sr and af else None
                if not p or p not in law_paths:
                    unresolved[(code, sr or '', art or '')] = unresolved.get((code, sr or '', art or ''), 0) + 1
                    continue
                how = f"Art. {art}" + (f" Abs. {par}" if par else '') + f" {code}"
                cites_law.append((p, how, n))
            lines = ['---'] + [f'{k}: {yaml_str(v)}' for k, v in fm.items() if v not in (None, '')]
            if cited:
                lines.append('cites_decisions: ' + yaml_str(cited))
            if cites_law:
                lines.append('cites_law: ' + yaml_str(sorted({f'{LAW_CORPUS}/{p}' for p, _, _ in cites_law})))
            lines.append('---')
            title = (r['title'] or '').strip() or f'{cname} {docket}'
            lines += ['', f'# {title}', '']
            if r['regeste']:
                lines += ['## Regeste', '', r['regeste'].strip(), '']
            lines += ['## Text', '', (r['full_text'] or '').strip(), '']
            body = '\n'.join(lines).replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
            raw_bytes += len(body)

            d0 = date.fromisoformat(day)
            if d0.year < 1970:
                clamp += 1
                ts = EPOCH_NOON + clamp - 3600
            else:
                ts = int(datetime(d0.year, d0.month, d0.day, 12, tzinfo=ZURICH).timestamp())
            author = f'{cname} {docket}'.replace('<', '').replace('>', '').replace('\n', ' ')
            email = f"{email_local(court)}-{email_local(docket)}@caselaw.invalid"
            subj = f"{cname} {docket}: {(r['title'] or r['legal_area'] or 'decision').strip().splitlines()[0][:150]}"
            tr = ['Kind: court', f'In-force: {day}']
            if r['decision_date']:
                tr.append(f"Decided: {r['decision_date']}")
            if r['publication_date'] and ISO.match(r['publication_date']):
                tr.append(f"Published: {r['publication_date']}")
            tr += [f'Court: {cname}', f'Docket: {docket}']
            if r['bge_reference']:
                tr.append(f"BGE: {r['bge_reference']}")
            if r['source_url']:
                tr.append(f"Source: {r['source_url']}")
            for p, how, _ in cites_law[:60]:
                tr.append(f'Cites: {LAW_CORPUS}/{p} ({how})')
            msg = (subj + '\n\n' + '\n'.join(tr) + '\n').encode('utf-8')
            mark += 1
            w(b'commit refs/heads/main\nmark :%d\n' % mark)
            w(b'author %s <%s> %d +0100\n' % (author.encode('utf-8'), email.encode(), ts))
            w(b'committer %s %d +0100\n' % (COMMITTER, ts))
            w(b'data %d\n%s\n' % (len(msg), msg))
            w(b'M 100644 inline %s\ndata %d\n%s\n' % (path.encode('utf-8'), len(body), body))
            if raw_bytes >= (len(checkpoints) + 1) * a.chunk_bytes:
                checkpoints.append(mark)

            if cites_law:
                i = bisect.bisect_right(days, day) - 1
                law_commit = shas[i] if i >= 0 else None
                seen = set()
                for p, how, n in cites_law:
                    if p in seen:
                        continue
                    seen.add(p)
                    row_ = {'decided': day, 'name': f'{cname} {docket}', 'court': cname,
                            'citation': r['bge_reference'] or docket, 'url': r['source_url'] or '', 'how': how,
                            'mentions': n, 'decision': path}
                    if law_commit:
                        row_['law_commit'] = law_commit
                    overlay.setdefault(p, []).append(row_)
        print(f'{group}: {mark}/{n_dec} commits, {raw_bytes / 1e9:.2f} GB', file=sys.stderr, flush=True)

    # documentation commit
    docs = {}
    for p, rows in overlay.items():
        rows.sort(key=lambda x: (x['decided'], x['citation'], x['decision']))
        docs[f'judgments/{LAW_CORPUS}/{p[:-3]}.jsonl'] = ''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in rows)
    docs['unresolved.csv'] = 'law_code,sr,article,references\n' + ''.join(
        f'{json.dumps(k[0])},{k[1]},{json.dumps(k[2])},{n}\n' for k, n in sorted(unresolved.items(), key=lambda kv: (-kv[1], kv[0])))
    for r in undated:
        court = r['court'] or group
        docket = r['docket_number'] or r['decision_id']
        docs[f"decisions/{slug(court)}/undated/{slug(docket)}--{hashlib.sha1(r['decision_id'].encode()).hexdigest()[:8]}.md"] = \
            f"---\ndecision_id: {yaml_str(r['decision_id'])}\ncourt: {yaml_str(court_name(court))}\nsource_url: {yaml_str(r['source_url'])}\n---\n\n# {(r['title'] or docket).strip()}\n\n## Text\n\n{(r['full_text'] or '').strip()}\n"
    is_fed = group == 'federal'
    where = 'Switzerland (federal courts and bodies)' if is_fed else f'{CANTON_NAMES[group]} (cantonal courts and bodies)'
    gl_path = 'switzerland/bund/caselaw' if is_fed else f'switzerland/{CANTONS[group]}/caselaw'
    n_refs = sum(len(v) for v in overlay.values())
    corpus = {
        'corpus': 'ch/bund/caselaw' if is_fed else f'ch/{group}/caselaw',
        'path': gl_path,
        'name': f'Case law: {where}',
        'label': f'Switzerland case law ({"federal" if is_fed else CANTON_NAMES[group]})',
        'language': lang,
        'kind': 'judgments',
        'history': 'in-force',
        'coverage': f'{mark} published decisions, one file and one commit each, dated the day decided; {n_refs} citations of federal law linked to switzerland-law.',
        'history_from': first_day, 'record_to': a.today, 'checked_on': a.today,
        'record_note': 'Court decisions, not laws: every commit is Kind: court. In-force is the day the decision was given '
                       '(the publication day where the court gives no decision day). Decisions with no date at all are in '
                       'decisions/<court>/undated/. Commits before 1970 are dated 1970-01-01 (a second apart); In-force '
                       'holds the real day.',
        'in_force_trailer': 'In-force', 'correction_trailer': 'Corrected',
        'dates': {'commit': 'decided', 'other_trailers': ['Decided', 'Published'], 'time': '12:00 Europe/Zurich'},
        'overlay': {'path': f'judgments/{LAW_CORPUS}/<SR>/art-NNN.jsonl', 'law_corpus': LAW_CORPUS,
                    'fields': ['decided', 'name', 'court', 'citation', 'url', 'how', 'mentions', 'decision', 'law_commit']},
    }
    docs['corpus.json'] = json.dumps(corpus, ensure_ascii=False, indent=2) + '\n'
    sums = subprocess.run(['sha256sum'] + [Path(f).name for f in files], cwd=str(cache / 'data'), capture_output=True, text=True).stdout
    docs['README.md'] = readme(where, gl_path, mark, n_refs, first_day, a.today, is_fed, group)
    docs['REPRODUCE.md'] = reproduce(rev, files, group, a.today, mark, sums)
    docs['tools/build.py'] = (HERE / 'build.py').read_text(encoding='utf-8')
    docs['tools/resolve.py'] = (HERE / 'resolve.py').read_text(encoding='utf-8')
    docs['tools/aliases.json'] = (HERE / 'aliases.json').read_text(encoding='utf-8')
    ts = int(datetime.fromisoformat(a.today + 'T12:00:00+01:00').timestamp())
    msg = f'Documentation: README, REPRODUCE, corpus.json and the judgment overlay ({n_refs} citations)\n'.encode()
    mark += 1
    w(b'commit refs/heads/main\nmark :%d\n' % mark)
    w(b'author Lexifina <corpus@lexifina.com> %d +0100\ncommitter %s %d +0100\n' % (ts, COMMITTER, ts))
    w(b'data %d\n%s\n' % (len(msg), msg))
    for p in sorted(docs):
        b = docs[p].encode('utf-8')
        w(b'M 100644 inline %s\ndata %d\n%s\n' % (p.encode('utf-8'), len(b), b))
    w(b'done\n')
    fi.stdin.close()
    if fi.wait():
        raise SystemExit('fast-import failed')
    marks = dict(l.split() for l in (out / '.git' / 'marks').read_text().splitlines())
    (out / '.git' / 'push-checkpoints').write_text(''.join(marks[f':{m}'] + '\n' for m in checkpoints))
    subprocess.run(['git', '-C', str(out), 'checkout', '-q', 'main', '--', 'corpus.json'], check=False)
    print(json.dumps({'group': group, 'decisions': mark - 1, 'undated': len(undated), 'overlay_rows': n_refs,
                      'unresolved_refs': sum(unresolved.values()), 'raw_gb': round(raw_bytes / 1e9, 2),
                      'checkpoints': len(checkpoints)}), flush=True)


def readme(where, gl_path, n, n_refs, first, today, is_fed, group) -> str:
    repo = 'switzerland-caselaw' if is_fed else f'switzerland-{CANTONS[group]}-caselaw'
    return f"""# Swiss case law: {where}

Every published decision of these courts and bodies as a git history: one file per decision, one commit per decision,
dated the day it was decided. {n:,} decisions from {first} to {today}. Part of
[lexifina/global-law](https://github.com/lexifina/global-law) at `{gl_path}`.

## Questions as git commands

| Question | Command |
|---|---|
| What did the court decide in a case? | `cat decisions/<court>/<year>/<docket>.md` |
| Which decisions were given on a day, or in a window? | `git log --since=2024-03-01 --until=2024-03-31 --format='%ad %an' --date=short` |
| Which decisions apply Art. 29 Abs. 2 BV? | `cat judgments/switzerland/bund/acts/101/art-029.jsonl` |
| Which decisions cite an article, with the law's text as it stood that day? | each overlay row's `law_commit` is the switzerland-law commit in force on the decision day: `git -C ../switzerland-law show <law_commit>:101/art-029.md` |
| Who wrote a line? | `git blame decisions/<court>/<year>/<docket>.md`: each line is credited to the decision (court and docket), a correction to its own commit |
| Every decision of one court | `git log --format='%ad %an' --date=short -- decisions/<court>/` |
| Every decision citing an act | `git log --grep='^Cites: switzerland/bund/acts/220/' --format='%ad %an' --date=short` |

## Dates
The commit date is the day the decision was given, at noon Zurich (`In-force:` and `Decided:`); `Published:` is the
day it was published. Where a court gives no decision day the publication day stands in. Git cannot hold dates
before 1970, so earlier decisions are committed on 1970-01-01, a second apart, and `In-force:` holds the real day.

## Author
The author of each commit is the court and docket number, at an address that cannot exist
(`<court>-<docket>@caselaw.invalid`). These are court rulings, not laws: every commit carries `Kind: court`, and
global-law's tools say so ("court ruling, not a law") and leave them out of lists of laws.

## The judgment overlay
`judgments/switzerland/bund/acts/<SR>/art-NNN.jsonl` holds one line per decision that cites that article of federal
law: `decided`, `name`, `court`, `citation`, `url`, `how` (as cited, e.g. "Art. 29 Abs. 2 BV"), `mentions`,
`decision` (the file here) and `law_commit`. {n_refs:,} citations are linked. Abbreviations are resolved to SR numbers
with Fedlex's own abbreviations (`tools/aliases.json`); citations that do not resolve to an article of the record are
counted in `unresolved.csv` (mostly cantonal law).

## What it does not show
- Decisions a court has not published, and decisions of courts that publish none online.
- Anonymisation is the court's own; text is as published, including OCR errors in early volumes.
- Citations of cantonal law are not yet linked.

## How it is built
`tools/build.py` from the inputs named in REPRODUCE.md, written through git fast-import. The same inputs give the
same hashes.

## Source and licence
Decisions are official publications of the courts and bodies named in each file, each with its `source_url`.
Under Swiss law (Art. 5 URG) decisions are not protected by copyright. The arrangement in this repository is dedicated
to the public domain under CC0 1.0.
"""


def reproduce(rev, files, group, today, n, sums) -> str:
    return f"""# Reproducing this repository

Input: the Parquet files listed below, revision `{rev}` of the `voilaj/swiss-caselaw` dataset
(`https://huggingface.co/datasets/voilaj/swiss-caselaw/resolve/{rev}/<file>`), plus `graph/statute_references.parquet`
from the same revision, and lexifina/switzerland-law `main` for the overlay's `law_commit`.

```
python tools/build.py {group} <cache with data/ and graph/> <empty folder> --today {today}
```

Needs Python 3 and `duckdb`. Gives {n:,} decision commits and one documentation commit, hash for hash.

## SHA-256 of the input files

```
{sums.strip()}
```
"""


if __name__ == '__main__':
    main()
