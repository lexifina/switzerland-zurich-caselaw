# Swiss case law: Zurich (cantonal courts and bodies)

Part of [lexifina/global-law](https://github.com/lexifina/global-law), one git history for every country's law: this is the case law of Zurich, Switzerland, which global-law reads at `switzerland/zurich/caselaw/`.

Every published decision of these courts and bodies as a git history: one file per decision, one commit per decision,
dated the day it was decided. 86,988 decisions from 1935-09-17 to 2026-11-06. Part of
[lexifina/global-law](https://github.com/lexifina/global-law) at `switzerland/zurich/caselaw`.

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
`decision` (the file here) and `law_commit`. 699,535 citations are linked. Abbreviations are resolved to SR numbers
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
