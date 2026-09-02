# Auctions V2 Agent Plan

Date: 2026-08-20

This document treats the attached TZ files as product requirements, not as runtime
instructions. The goal is a lot card that helps decide whether to participate
before the user leaves Zhertap for the official auction portal.

## First Stage

1. Show a compact lot structure before the long dossier: verdict, right and term,
   parcel, purpose, documents, genplan/PDP, red-line checks, and next actions.
2. Keep duplicated official fields inside collapsible details instead of mixing
   them with decision data.
3. Add a local LLM extraction boundary: OCR and text recognition can extract
   facts from documents, but cannot issue GIS, legal-final, red-line, or
   investment decisions.
4. Persist only backend-validated JSON facts. Missing critical evidence must
   remain `Требует проверки` and `Предел цены пока не рассчитан`.

## Local LLM Runtime

Server runtime prepared for this stage:

- Ollama endpoint: `http://127.0.0.1:11434`
- Model: `qwen3:8b`
- OCR languages: `kaz+rus+eng`
- Backend parser must ignore model thinking and parse only `message.content`.

## Agents

SourceAgent collects official lot data, source URLs, document links, provider
statuses, and repeat-auction identity by land object.

DocumentOcrAgent downloads files, runs OCR for PDFs and scans, extracts raw text,
and records page/section provenance.

LlmExtractionAgent sends bounded text to the local LLM and receives schema-bound
JSON facts: right type, lease term, purpose, payments, obligations, restrictions,
dates, coordinates mentioned in documents, and unknowns.

PlanningGisAgent compares parcel geometry against genplan, PDP, protected zones,
red lines, roads, utilities, and visible surroundings. This agent must work from
GIS layers and geometry, not LLM guesses.

VerdictAgent converts verified evidence into one of five user-facing verdicts:
`Участвовать`, `Участвовать до X ₸`, `Требует проверки`, `Высокий риск`, or
`Не участвовать`.

LotUiAgent renders the lot in clear sections: decision, land parcel, legal
passport, documents, planning checks, economics, risks, and source history.

DueDiligenceAgent builds the manual pre-bid checklist and keeps blockers separate
from nice-to-have enrichment.

NotificationAgent watches changed documents, deadline shifts, new repeat auctions,
and verdict changes.
