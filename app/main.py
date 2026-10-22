import hmac
from collections import Counter
import logging
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case as case_, func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from app import appsettings, cases, help as helptexts, learning, receipts, rules as rule_proposals, deadlines, events, fields, highlight, ics, metadata, notify, payments, pipeline, reminders, retention, selection, tax
from app.config import Settings, get_settings
from app.db import get_session, init_db, session_scope
from app.models import Case, CaseDocument, CaseEntry, Contract, Document, Invoice, KeyValue, ReceiptCheck, RuleProposal, RetentionLog, RetentionRule, Warranty
from app.firefly import FireflyError
from app.paperless import PaperlessError
from app.version import Version
from app.worker import Worker, make_firefly, make_ollama, make_paperless

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
BASE = Path(__file__).parent
PUBLIC_PATHS = ("/login", "/static", "/favicon.ico", "/api/webhook", "/calendar.ics", "/health")


def create_app(settings: Settings | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    # Werte aus der .env, falls die in der App gespeicherte Zuordnung zurückgesetzt wird
    env_doctypes = {k: getattr(settings, k) for k in ("doctypes_invoice", "doctypes_contract", "doctypes_warranty", "tax_doctypes")}
    # Werte aus der .env für die übrigen in der App änderbaren Einstellungen
    env_settings = {name: getattr(settings, name) for name in appsettings.FIELDS}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_writable(settings.data_dir)
        init_db(settings.database_url)
        with session_scope() as s:
            retention.seed_defaults(s)
            pipeline.apply_doctype_settings(s, settings)
            appsettings.apply(s, settings)
        settings_changed()
        worker = Worker(settings, version)
        app.state.worker = worker
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    version = Version(BASE.parent, settings.update_repo, settings.update_check, settings.update_github_token)
    app = FastAPI(title="Paperless Companion", lifespan=lifespan)
    app.state.settings = settings
    app.state.version = version
    templates = Jinja2Templates(directory=BASE / "templates")
    templates.env.filters["d"] = events.fmt_date
    templates.env.filters["eur"] = events.fmt_money

    def fmt_ts(value) -> str:
        """Zeitpunkt (ISO-Text oder datetime, UTC) in lokaler Zeit: 21.10.2026 09:35."""
        if not value:
            return "–"
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m.%Y %H:%M")

    templates.env.filters["ts"] = fmt_ts
    templates.env.globals.update(page_help=helptexts.page_help, TIPS=helptexts.TIPS, asset_v=asset_version())
    templates.env.globals["tx_links"] = lambda group_id, day: payments.links(settings, group_id, day)

    def payment_of(doc: Document) -> dict | None:
        with session_scope() as s:
            return payments.payment_for(s, s.get(Document, doc.id))

    templates.env.globals["payment_of"] = payment_of
    templates.env.globals.update(HINTS=metadata.HINTS, META_SOURCES=metadata.SOURCES, KINDS=pipeline.KINDS, link_base=settings.paperless_link_base, auth=bool(settings.app_password))

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if settings.app_password and not path.startswith(PUBLIC_PATHS) and not request.session.get("auth"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    # Muss nach require_login registriert werden, damit request.session dort verfügbar ist
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 30, same_site="lax")
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.middleware("http")
    async def static_revalidate(request: Request, call_next):
        """CSS/JS bei jedem Aufruf kurz prüfen lassen (ETag), damit nach Updates nie alte Dateien verwendet werden."""
        response = await call_next(request)
        if request.url.path.startswith("/static/") and request.url.path.endswith((".css", ".js")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def today() -> date:
        return datetime.now(ZoneInfo(settings.timezone)).date()

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        with session_scope() as s:
            inbox_count = s.scalar(select(func.count()).select_from(Document).where(Document.review_state == "pending"))
            outgoing = s.scalar(select(func.count()).select_from(Document).where(
                Document.writeback_pending.is_(True) | Document.meta_pending.is_(True)))
        return templates.TemplateResponse(request, name, {
            "inbox_count": inbox_count, "outgoing_count": outgoing, "today": today(),
            "update_available": app.state.version.update_available, **ctx,
        })

    def settings_changed() -> None:
        """Nach Änderungen in der App: abhängige Objekte nachziehen (der Worker verbindet sich selbst neu)."""
        templates.env.globals["link_base"] = settings.paperless_link_base
        version.repo, version.token = settings.update_repo, settings.update_github_token
        version.enabled = settings.update_check and bool(settings.update_repo)

    def check_token(expected: str, given: str | None) -> None:
        if not expected or not given or not hmac.compare_digest(expected, given):
            raise HTTPException(status_code=403, detail="Ungültiges Token")

    # ------------------------------------------------------------------ Login

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        return render(request, "help.html", PAGES=helptexts.PAGES, GLOSSARY=helptexts.GLOSSARY, STEPS=helptexts.STEPS)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(BASE / "static" / "icons" / "favicon.ico", media_type="image/x-icon")

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        if settings.app_password and hmac.compare_digest(password, settings.app_password):
            request.session["auth"] = True
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": "Falsches Passwort"}, status_code=401)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------------ Seiten

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: Session = Depends(get_session)):
        t = today()
        all_events = events.collect(session, t)
        upcoming = [e for e in all_events if (e.date - t).days <= 60]
        open_invoices = session.scalars(select(Invoice).where(Invoice.status == "open")).all()
        stats = {
            "open_sum": round(sum(i.amount or 0 for i in open_invoices if i.payment_method != "debit"), 2),
            "open_count": len([i for i in open_invoices if i.payment_method != "debit"]),
            "overdue": len([e for e in all_events if e.type == "overdue"]),
            "fixed_costs": events.monthly_fixed_costs(session),
            "payment_suggestions": len(payments.suggestions(session)),
            "contracts": session.scalar(select(func.count()).select_from(Contract).where(Contract.status == "active")),
            "queued": session.scalar(
                select(func.count()).select_from(Document).where(Document.extraction_state.in_(("queued", "running")))
            ),
        }
        return render(request, "dashboard.html", events=upcoming, stats=stats)

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request, msg: str = "", session: Session = Depends(get_session)):
        docs = session.scalars(
            select(Document).where(Document.review_state == "pending").order_by(Document.created.desc())
        ).all()
        return render(request, "inbox.html", docs=docs, msg=msg)

    @app.get("/documents", response_class=HTMLResponse)
    def documents(request: Request, msg: str = "", session: Session = Depends(get_session)):
        flt = selection.Filter.from_mapping(request.query_params)
        docs = flt.apply(session)
        return render(
            request, "documents.html", docs=docs[:500], total=len(docs), flt=flt, msg=msg,
            facets=selection.facets(session), settings_meta_mode=settings.meta_mode, METHODS=pipeline.METHODS, method_of=pipeline.analysis_method,
        )

    @app.post("/documents/analyze")
    async def documents_analyze(request: Request, session: Session = Depends(get_session)):
        """Ausgewählte oder alle gefilterten Dokumente zur Analyse einreihen."""
        form = await request.form()
        flt = selection.Filter.from_mapping(form)
        if form.get("scope") == "selected":
            ids = [int(i) for i in form.getlist("doc") if str(i).isdigit()]
            docs = [d for d in (session.get(Document, i) for i in ids) if d is not None]
        else:
            docs = flt.apply(session)
        skip_confirmed = form.get("include_confirmed") != "1"
        count = 0
        for doc in docs:
            if skip_confirmed and doc.review_state == "confirmed":
                continue
            if doc.extraction_state not in ("queued", "running"):
                doc.extraction_state = "queued"
                count += 1
        request_worker(app)
        msg = f"{count} Dokumente zur Analyse eingereiht"
        if skip_confirmed and any(d.review_state == "confirmed" for d in docs):
            msg += " (bereits bestätigte übersprungen)"
        query = flt.query_string()
        return RedirectResponse(f"/documents?{query}{'&' if query else ''}msg={quote(msg)}", status_code=303)

    @app.post("/documents/suggest-meta")
    async def documents_suggest_meta(request: Request, session: Session = Depends(get_session)):
        """Für ausgewählte oder alle gefilterten Dokumente Metadaten-Vorschläge anfordern."""
        form = await request.form()
        flt = selection.Filter.from_mapping(form)
        if form.get("scope") == "selected":
            ids = [int(i) for i in form.getlist("doc") if str(i).isdigit()]
            docs = [d for d in (session.get(Document, i) for i in ids) if d is not None]
        else:
            docs = flt.apply(session)
        count = 0
        for doc in docs:
            if doc.meta_state not in ("queued", "running"):
                doc.meta_state = "queued"
                count += 1
        request_worker(app)
        msg = f"{count} Dokumente für Metadaten-Vorschläge eingereiht"
        query = flt.query_string()
        return RedirectResponse(f"/documents?{query}{'&' if query else ''}msg={quote(msg)}", status_code=303)

    @app.post("/documents/{doc_id}/meta")
    async def document_meta(doc_id: int, request: Request, session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404)
        form = await request.form()
        action, keys = form.get("action"), [str(k) for k in form.getlist("item")]
        if action == "suggest":
            doc.meta_state = "queued"
            request_worker(app)
            msg = "Metadaten-Vorschlag angefordert"
        elif action in ("accept", "dismiss", "withdraw", "transfer"):
            msg = meta_action(session, action, {doc_id: keys}, {doc_id: str(form.get("title") or "")})
        else:
            raise HTTPException(400)
        return RedirectResponse(f"/documents/{doc_id}?msg={quote(msg)}#metadata", status_code=303)

    def meta_action(session: Session, action: str, selected: dict[int, list[str]], titles: dict[int, str] | None = None) -> str:
        """Vormerken/Verwerfen/Zurücknehmen nur im Companion; nur „transfer“ schreibt nach Paperless."""
        titles = titles or {}
        docs = {i: d for i in selected if (d := session.get(Document, i)) is not None}
        if action == "accept":
            n = sum(metadata.accept(session, d, selected[i], titles.get(i)) for i, d in docs.items())
            return f"{n} Vorschläge vorgemerkt – noch nicht in Paperless"
        if action == "dismiss":
            return f"{sum(metadata.dismiss(session, d, selected[i]) for i, d in docs.items())} Vorschläge verworfen"
        if action == "withdraw":
            return f"{sum(metadata.withdraw(session, d, selected[i]) for i, d in docs.items())} Vormerkungen zurückgenommen"
        done, errors = 0, []
        paperless = make_paperless(settings, timeout=30)
        try:
            for doc_id, doc in docs.items():
                old_type = doc.document_type
                try:
                    done += len(metadata.transfer(session, paperless, doc, selected[doc_id]))
                except PaperlessError as exc:
                    errors.append(f"#{doc_id}: {exc}")
                    continue
                # Neuer Dokumenttyp kann die Art ändern -> unbestätigte Analyse neu einreihen
                if doc.document_type != old_type and doc.extraction_state == "done" \
                        and doc.review_state != "confirmed" and doc.kind_source not in ("manual", "fields"):
                    doc.extraction_state = "queued"
                    request_worker(app)
        finally:
            paperless.close()
        return f"{done} vorgemerkte Änderungen nach Paperless übertragen" + (f". Probleme: {'; '.join(errors[:5])}" if errors else "")

    @app.get("/documents/{doc_id}", response_class=HTMLResponse)
    def document_detail(request: Request, doc_id: int, msg: str = "", session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument nicht gefunden")
        values, from_record = form_values(doc)
        src = field_badges(doc, from_record)
        deadline = None
        if doc.contract:
            deadline = deadlines.compute(deadlines.terms_from_contract(doc.contract), today())
        open_cases = session.scalars(select(Case).where(Case.status == "open").order_by(Case.title)).all()
        text_html, text_found, text_error = None, set(), None
        paperless = make_paperless(settings, timeout=15)
        try:
            content = paperless.get_document(doc_id).get("content") or ""
            text_html, text_found = highlight.highlight(content, doc.suggestions or {})
        except PaperlessError as exc:
            text_error = str(exc)
        finally:
            paperless.close()
        return render(
            request, "document.html", doc=doc, values=values, from_record=from_record, src=src,
            SOURCE_LABELS=pipeline.SOURCE_LABELS,
            sugg=doc.suggestions or {}, deadline=deadline,
            doc_cases=cases.cases_for_document(session, doc_id), open_cases=open_cases,
            related=[(i, session.get(Document, i)) for i in sorted(pipeline.related_ids(session, doc_id))],
            pf=doc.paperless_fields or {}, settings=settings, msg=msg,
            text_html=text_html, text_found=text_found, text_error=text_error, HL_LABELS=highlight.LABELS,
            meta_items=metadata.pending(doc, everything=True), META_SOURCES=metadata.SOURCES,
            pending_left=session.scalar(
                select(func.count()).select_from(Document).where(Document.review_state == "pending", Document.id != doc_id)
            ),
        )

    @app.get("/documents/{doc_id}/preview")
    def document_preview(doc_id: int, kind: str = "preview"):
        """Vorschau aus Paperless durchreichen – der Paperless-Token bleibt auf dem Server."""
        if kind not in ("preview", "thumb"):
            raise HTTPException(404)
        paperless = make_paperless(settings, timeout=60)
        try:
            content, content_type = paperless.preview(doc_id, kind)
        except PaperlessError as exc:
            raise HTTPException(502, f"Vorschau nicht verfügbar: {exc}") from exc
        finally:
            paperless.close()
        return Response(content, media_type=content_type, headers={
            "Content-Disposition": "inline", "Cache-Control": "private, max-age=600",
        })

    @app.post("/documents/{doc_id}/confirm")
    async def document_confirm(request: Request, doc_id: int, session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None or doc.kind not in pipeline.FIELD_TYPES:
            raise HTTPException(404)
        form = dict(await request.form())
        before = form_values(doc)[0] if doc.review_state == "confirmed" else {}
        before_sources = dict(doc.field_sources or {})
        first_confirmation = doc.review_state == "pending"
        values = pipeline.parse_values(doc.kind, form)
        if first_confirmation:
            learning.record_accuracy(session, doc, doc.kind, values)  # stimmten die Vorschläge?
        pipeline.apply_values(session, doc, doc.kind, values)
        pipeline.record_confirmation(doc, doc.kind, values, before, before_sources)
        doc.review_state = "confirmed"
        doc.auto_confirmed = False
        payments.record_confirmed_payment(session, doc)
        doc.writeback_pending = True
        request_worker(app)
        if form.get("after") == "next":
            following = session.scalars(
                select(Document).where(Document.review_state == "pending", Document.id != doc_id)
                .order_by(Document.created.desc()).limit(1)
            ).first()
            if following is not None:
                return RedirectResponse(f"/documents/{following.id}?msg={quote('Bestätigt – nächstes Dokument')}", status_code=303)
            return RedirectResponse(f"/inbox?msg={quote('Bestätigt – die Inbox ist leer')}", status_code=303)
        target = form.get("next") or "/inbox"
        return RedirectResponse(target if target.startswith("/") else "/inbox", status_code=303)

    @app.post("/documents/{doc_id}/kind")
    def document_kind(doc_id: int, kind: str = Form(...), session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None or kind not in pipeline.KINDS:
            raise HTTPException(404)
        doc.kind, doc.kind_source = kind, "manual"
        doc.extraction_state = "queued"
        doc.review_state = "none"
        request_worker(app)
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    @app.post("/documents/{doc_id}/reanalyze")
    def document_reanalyze(doc_id: int, session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404)
        doc.extraction_state = "queued"
        request_worker(app)
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    @app.post("/documents/{doc_id}/comment")
    def document_comment(doc_id: int, comment: str = Form(""), session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404)
        pipeline.propose(doc, "comment", comment.strip() or None)
        msg = "Kommentar vorgemerkt – unter „Für Paperless“ übernehmen"
        return RedirectResponse(f"/documents/{doc_id}?msg={quote(msg)}", status_code=303)

    @app.post("/documents/{doc_id}/summary")
    def document_summary(doc_id: int, session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404)
        doc.summary_state = "queued"
        request_worker(app)
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    @app.post("/documents/{doc_id}/case-from-links")
    def document_case_from_links(doc_id: int, session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404)
        case = Case(title=doc.title or f"Vorgang zu #{doc_id}")
        session.add(case)
        session.flush()
        for other_id in [doc_id, *sorted(pipeline.related_ids(session, doc_id))]:
            if session.get(Document, other_id) is not None:
                cases.add_document(session, case, session.get(Document, other_id))
        return RedirectResponse(f"/cases/{case.id}", status_code=303)

    @app.post("/system/queue/{action}")
    def system_queue(action: str, session: Session = Depends(get_session)):
        if action == "pause":
            pipeline.kv_set(session, "queue_paused", "1")
            msg = "Warteschlange angehalten – laufende Analyse wird noch beendet"
        elif action == "resume":
            pipeline.kv_set(session, "queue_paused", "0")
            request_worker(app)
            msg = "Warteschlange läuft weiter"
        elif action == "clear":
            msg = f"{pipeline.clear_queue(session)} wartende Aufträge zurückgenommen"
        else:
            raise HTTPException(404)
        return RedirectResponse(f"/system?msg={quote(msg)}", status_code=303)

    @app.post("/system/analyze-known-types")
    def system_analyze_known_types(session: Session = Depends(get_session)):
        docs = pipeline.index_documents_with_known_type(session, settings)
        for doc in docs:
            doc.extraction_state = "queued"
        request_worker(app)
        msg = f"{len(docs)} Dokumente mit bekanntem Dokumenttyp zur Analyse eingereiht"
        return RedirectResponse(f"/system?msg={quote(msg)}", status_code=303)

    @app.post("/system/reanalyze-outdated")
    def system_reanalyze_outdated(session: Session = Depends(get_session)):
        docs = pipeline.outdated_documents(session)
        for doc in docs:
            doc.extraction_state = "queued"
        request_worker(app)
        return RedirectResponse(f"/system?msg={quote(f'{len(docs)} Dokumente werden mit der aktuellen Erkennung neu analysiert')}", status_code=303)

    @app.post("/system/retry-llm")
    def system_retry_llm(session: Session = Depends(get_session)):
        count = pipeline.requeue_llm_errors(session, include_exhausted=True)
        request_worker(app)
        return RedirectResponse(f"/system?msg={quote(f'{count} Dokumente werden mit AI neu analysiert')}", status_code=303)

    @app.post("/system/summaries")
    def system_summaries(session: Session = Depends(get_session)):
        count = 0
        for doc in session.scalars(select(Document).where(Document.summary_state.is_(None), Document.summary.is_(None))):
            if not (doc.paperless_fields or {}).get("summary"):
                doc.summary_state = "queued"
                count += 1
        request_worker(app)
        return RedirectResponse(f"/system?msg={quote(f'{count} Dokumente für die Zusammenfassung eingereiht')}", status_code=303)

    @app.post("/documents/{doc_id}/ignore")
    def document_ignore(doc_id: int, session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404)
        doc.kind, doc.kind_source, doc.review_state = "other", "manual", "none"
        doc.contract = doc.invoice = doc.warranty = None
        return RedirectResponse("/inbox", status_code=303)

    @app.get("/contracts", response_class=HTMLResponse)
    def contracts(request: Request, session: Session = Depends(get_session)):
        t = today()
        rows = []
        for c in session.scalars(select(Contract)):
            rows.append((c, deadlines.compute(deadlines.terms_from_contract(c), t)))
        rows.sort(key=lambda r: (r[0].status != "active", r[1].cancel_by or date.max))
        return render(request, "contracts.html", rows=rows, fixed_costs=events.monthly_fixed_costs(session))

    @app.post("/contracts/{contract_id}/cancelled")
    def contract_cancelled(contract_id: int, cancelled_on: str = Form(""), session: Session = Depends(get_session)):
        c = session.get(Contract, contract_id)
        if c is None:
            raise HTTPException(404)
        c.status = "cancelled"
        c.cancelled_on = pipeline.coerce("date", cancelled_on) or today()
        c.document.writeback_pending = True
        pipeline.set_source(c.document, ["status", "cancelled_on"], "manual", confirmed=True)
        return RedirectResponse("/contracts", status_code=303)

    @app.get("/invoices", response_class=HTMLResponse)
    def invoices(request: Request, status: str = "open", msg: str = "", session: Session = Depends(get_session)):
        query = select(Invoice).order_by(Invoice.due_date.asc().nulls_last())
        if status in ("open", "paid"):
            query = query.where(Invoice.status == status)
        return render(
            request, "invoices.html", invoices=session.scalars(query).all(), status=status,
            matches=payments.suggestions(session), msg=msg, SOURCE_LABELS=pipeline.SOURCE_LABELS,
            firefly_enabled=settings.firefly_enabled,
        )

    @app.post("/payments/run")
    def payments_run(session: Session = Depends(get_session)):
        client = make_firefly(settings)
        if client is None:
            return RedirectResponse(f"/invoices?msg={quote('Firefly III ist nicht konfiguriert')}", status_code=303)
        try:
            counts = payments.run(session, client, settings, today())
            if not counts["open"] and not counts["pending"]:
                msg = "Keine offenen Rechnungen – nichts abzugleichen"
            elif not counts["open"]:
                msg = f"{counts['transactions']} Abbuchungen für {counts['pending']} Rechnungen in der Inbox geprüft"
            elif not counts["open_with_amount"]:
                msg = f"{counts['open']} offene Rechnungen, aber keine mit Betrag – ohne Betrag kann nichts zugeordnet werden"
            else:
                msg = (f"{counts['transactions']} Abbuchungen für {counts['open_with_amount']} offene Rechnungen geprüft: "
                       f"{counts['suggested']} Vorschläge, {counts['accepted']} automatisch als bezahlt markiert")
            if counts["pending_found"]:
                msg += f" · {counts['pending_found']} Zahlungen für Rechnungen in der Inbox vorbelegt"
        except FireflyError as exc:
            msg = f"Fehler beim Abgleich: {exc}"
        finally:
            client.close()
        return RedirectResponse(f"/invoices?msg={quote(msg)}", status_code=303)

    @app.post("/payments/{match_id}/{decision}")
    def payments_decide(match_id: int, decision: str, session: Session = Depends(get_session)):
        if decision == "accept":
            found = payments.accept(session, match_id)
        elif decision == "reject":
            found = payments.reject(session, match_id)
        else:
            raise HTTPException(404)
        if found is None:
            raise HTTPException(404)
        request_worker(app)
        return RedirectResponse("/invoices", status_code=303)

    @app.post("/invoices/{invoice_id}/paid")
    def invoice_paid(invoice_id: int, session: Session = Depends(get_session)):
        inv = session.get(Invoice, invoice_id)
        if inv is None:
            raise HTTPException(404)
        inv.status, inv.paid_on = "paid", today()
        inv.document.writeback_pending = True
        pipeline.set_source(inv.document, ["status", "paid_on"], "manual", confirmed=True)
        request_worker(app)
        return RedirectResponse("/invoices", status_code=303)

    @app.post("/invoices/{invoice_id}/open")
    def invoice_open(invoice_id: int, session: Session = Depends(get_session)):
        inv = session.get(Invoice, invoice_id)
        if inv is None:
            raise HTTPException(404)
        inv.status, inv.paid_on = "open", None
        inv.document.writeback_pending = True
        pipeline.set_source(inv.document, "status", "manual", confirmed=True)
        pipeline.clear_source(inv.document, "paid_on")
        request_worker(app)
        return RedirectResponse("/invoices?status=paid", status_code=303)

    @app.get("/warranties", response_class=HTMLResponse)
    def warranties(request: Request, session: Session = Depends(get_session)):
        rows = sorted(session.scalars(select(Warranty)).all(), key=lambda w: w.expires_on or date.max)
        return render(request, "warranties.html", rows=rows)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, msg: str = "", session: Session = Depends(get_session)):
        paperless = make_paperless(settings, timeout=10)
        error, diagnosis = None, None
        try:
            names = sorted(paperless.names("document_types").values(), key=str.lower)
            if not names:
                diagnosis = doctype_diagnosis(paperless.current_user())
        except PaperlessError as exc:
            names, error = [], str(exc)
        finally:
            paperless.close()
        if not names:
            # Rückgriff: Typen, die im lokalen Index an Dokumenten stehen
            names = sorted({t for t in session.scalars(select(Document.document_type).distinct()) if t}, key=str.lower)
        counts = {(name or "").lower(): (total, analyzed) for name, total, analyzed in session.execute(
            select(Document.document_type, func.count(),
                   func.sum(case_((Document.extraction_state != "skipped", 1), else_=0))).group_by(Document.document_type)
        )}
        choices = pipeline.doctype_choices(settings)
        # Zugeordnete Typen, die es in Paperless nicht mehr gibt, nur zeigen, solange noch Dokumente sie tragen
        known = {n.lower() for n in names}
        missing = sorted(n for n in set(choices["kinds"]) | set(choices["tax"])
                         if n not in known and counts.get(n, (0, 0))[0]) if not error else []
        rows = [{"name": n, "key": n.lower(), "kind": choices["kinds"].get(n.lower(), ""),
                 "tax": n.lower() in choices["tax"], "count": counts.get(n.lower(), (0, 0)), "missing": n in missing}
                for n in [*names, *missing]]
        overrides = appsettings.stored(session)
        return render(request, "settings.html", rows=rows, error=error, msg=msg, diagnosis=diagnosis,
                      unresolved_tags=pipeline.unresolved_tag_count(session),
                      SECTIONS=appsettings.SECTIONS, CHOICES=appsettings.CHOICES, field_kind=appsettings.kind,
                      settings=settings, overrides=overrides, env_settings=env_settings,
                      stored=pipeline.kv_get(session, "doctype_settings") is not None,
                      index_known_type=len(pipeline.index_documents_with_known_type(session, settings)))

    def doctype_diagnosis(user: dict) -> str:
        """Warum liefert Paperless keine Dokumenttypen? Meist fehlen dem API-Benutzer Leserechte."""
        name = f"„{user['username']}“" if user.get("username") else "des API-Tokens"
        needed = {"view_documenttype": "Dokumenttyp", "view_correspondent": "Korrespondent", "view_tag": "Tag",
                  "view_customfield": "Benutzerdefiniertes Feld"}
        missing = [label for perm, label in needed.items() if perm not in user.get("permissions", [])]
        if user and not user.get("is_superuser") and "Dokumenttyp" in missing:
            return (f"Dem Paperless-Benutzer {name} fehlt das Recht „Anzeigen“ für: {', '.join(missing)}. "
                    "In Paperless unter Einstellungen → Benutzer & Gruppen beim Benutzer (oder seiner Gruppe) "
                    "jeweils „Anzeigen“ setzen.")
        return (f"Paperless hat für den Benutzer {name} keine Dokumenttypen geliefert. Entweder sind keine angelegt, "
                "oder sie gehören einem anderen Benutzer und sind nicht für diesen freigegeben – in Paperless unter "
                "Dokumenttypen beim jeweiligen Typ „Berechtigungen“ den Benutzer bei „Anzeigen“ eintragen "
                "oder den Typ ohne Eigentümer anlegen.")

    @app.post("/settings/app")
    async def settings_app(request: Request, session: Session = Depends(get_session)):
        form = await request.form()
        changed, errors = appsettings.save(session, settings, env_settings, form)
        settings_changed()
        labels = [appsettings.FIELDS[n][0] for n in changed]
        msg = f"Gespeichert: {', '.join(labels)}" if labels else "Keine Änderungen"
        if errors:
            msg += ". Nicht übernommen: " + "; ".join(errors)
        if changed:
            app.state.worker.wake.set()
        return RedirectResponse(f"/settings?msg={quote(msg)}#app-settings", status_code=303)

    @app.post("/settings/app/reset")
    def settings_app_reset(name: str = Form(""), session: Session = Depends(get_session)):
        names = appsettings.reset(session, settings, env_settings, [name] if name else None)
        settings_changed()
        msg = f"{len(names)} Einstellungen wieder aus der .env übernommen"
        return RedirectResponse(f"/settings?msg={quote(msg)}#app-settings", status_code=303)

    @app.post("/settings/reindex")
    def settings_reindex(back: str = Form("/settings"), session: Session = Depends(get_session)):
        """Archiv-Index neu einlesen, z.B. nachdem in Paperless Leserechte ergänzt wurden."""
        paperless = make_paperless(settings, timeout=60)
        try:
            total, _ = pipeline.index_sync(session, paperless, settings)
            missing, _ = pipeline.unresolved_tag_count(session)
            msg = f"Archiv-Index neu eingelesen: {total} Dokumente" + (
                f" – {missing} Dokumente haben weiterhin Tags ohne Namen (Leserechte prüfen)" if missing else "")
        except PaperlessError as exc:
            msg = f"Fehler: {exc}"
        finally:
            paperless.close()
        back = back if back in ("/settings", "/paperless-changes", "/system") else "/settings"
        return RedirectResponse(f"{back}?msg={quote(msg)}", status_code=303)

    @app.post("/settings/doctypes")
    async def settings_doctypes(request: Request, session: Session = Depends(get_session)):
        form = await request.form()
        names = form.getlist("name")
        kinds = {n: form.get(f"kind:{n}", "") for n in names}
        tax_types = [n for n in names if form.get(f"tax:{n}")]
        changed = pipeline.save_doctype_settings(session, settings, kinds, tax_types)
        requeued = pipeline.requeue_doctypes(session, changed)
        if requeued:
            request_worker(app)
        msg = "Zuordnung gespeichert"
        if requeued:
            msg += f" – {requeued} bereits analysierte Dokumente werden mit der neuen Art neu analysiert"
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    @app.post("/settings/doctypes/reset")
    def settings_doctypes_reset(session: Session = Depends(get_session)):
        item = session.get(KeyValue, "doctype_settings")
        if item:
            session.delete(item)
        for key, value in env_doctypes.items():
            setattr(settings, key, value)
        return RedirectResponse(f"/settings?msg={quote('Zuordnung aus der .env wiederhergestellt')}", status_code=303)

    @app.get("/system", response_class=HTMLResponse)
    def system(request: Request, msg: str = "", session: Session = Depends(get_session)):
        paperless = make_paperless(settings, timeout=5)
        try:
            paperless_status = {"ok": True, "error": None, **paperless.ping()}
        except PaperlessError as exc:
            paperless_status = {"ok": False, "error": str(exc)}
        finally:
            paperless.close()
        ollama_status = None
        if (ollama := make_ollama(settings)) is not None:
            ollama_status = ollama.status()
            ollama.close()
        firefly_status = None
        if (firefly := make_firefly(settings)) is not None:
            try:
                about = firefly.ping()
                firefly_status = {"ok": True, "version": about.get("version", "?")}
            except FireflyError as exc:
                firefly_status = {"ok": False, "error": str(exc)}
            finally:
                firefly.close()
        worker: Worker = app.state.worker
        counts = dict(session.execute(
            select(Document.extraction_state, func.count()).group_by(Document.extraction_state)
        ).all())
        return render(
            request, "system.html", settings=settings, paperless_status=paperless_status,
            ollama_status=ollama_status, firefly_status=firefly_status, counts=counts, msg=msg,
            last_sync=pipeline.kv_get(session, "last_sync"), channels=notify.channels(settings),
            version=app.state.version.get(), llm_error_count=len(pipeline.documents_with_llm_error(session, True)),
            outdated_count=len(pipeline.outdated_documents(session)), analysis_version=pipeline.ANALYSIS_VERSION,
            queue_paused=pipeline.kv_get(session, "queue_paused") == "1", **activity_context(),
            overview=pipeline.analysis_overview(session, settings),
            payment_summary=payments.summary(session) if settings.firefly_enabled else None,
            summaries_queued=session.scalar(select(func.count()).select_from(Document).where(Document.summary_state == "queued")),
            llm_now=settings.llm_allowed_at(datetime.now(ZoneInfo(settings.timezone)).hour),
        )

    def activity_context() -> dict:
        worker: Worker = app.state.worker
        loaded = None
        if (ollama := make_ollama(settings)) is not None:
            loaded = ollama.loaded()
            ollama.close()
        running_for = None
        if worker.current_since is not None:
            running_for = int(time.monotonic() - worker.current_since)
        return {
            "worker": worker, "activity": list(worker.activity)[:20], "stats": worker.stats,
            "ollama_loaded": loaded, "running_for": running_for, "llm_enabled": settings.llm_enabled,
        }

    @app.get("/system/activity", response_class=HTMLResponse)
    def system_activity(request: Request):
        return templates.TemplateResponse(request, "activity.html", activity_context())

    @app.post("/system/version-check")
    def system_version_check():
        app.state.version.refresh(force=True)
        return RedirectResponse("/system#version", status_code=303)

    @app.post("/system/update")
    def system_update():
        if not settings.update_webhook_url:
            raise HTTPException(400, "UPDATE_WEBHOOK_URL ist nicht gesetzt")
        info = app.state.version.get()
        headers = {}
        if settings.update_webhook_secret:
            headers[settings.update_webhook_header] = settings.update_webhook_secret
        payload = {
            "typ": "update",
            "von": info["current"].get("commit"),
            "auf": (info.get("latest") or {}).get("commit"),
        }
        try:
            response = httpx.post(settings.update_webhook_url, json=payload, headers=headers, timeout=20)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "status": response.status_code, "von": payload["von"]}

    @app.get("/api/version")
    def api_version():
        return app.state.version.get()

    @app.post("/system/sync")
    def system_sync():
        app.state.worker.request_sync()
        return RedirectResponse("/system?msg=Synchronisation+gestartet", status_code=303)

    @app.post("/system/import")
    def system_import(doc_id: int = Form(...), session: Session = Depends(get_session)):
        paperless = make_paperless(settings)
        try:
            pipeline.import_single(session, paperless, doc_id, settings)
        except PaperlessError as exc:
            return RedirectResponse(f"/system?msg={quote(f'Import fehlgeschlagen: {exc}')}", status_code=303)
        finally:
            paperless.close()
        request_worker(app)
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    @app.post("/system/test-notification")
    def system_test_notification():
        try:
            used = notify.send(settings, "Paperless Companion: Test", "Testnachricht – Benachrichtigungen funktionieren.")
            msg = "Test gesendet über " + ", ".join(used)
        except notify.NotifyError as exc:
            msg = f"Fehler: {exc}"
        return RedirectResponse(f"/system?msg={quote(msg)}", status_code=303)

    @app.post("/system/reminders")
    def system_reminders(session: Session = Depends(get_session)):
        try:
            count = reminders.run(session, settings, today())
            msg = f"{count} Erinnerungen gesendet" if count else "Keine fälligen Erinnerungen"
        except notify.NotifyError as exc:
            msg = f"Fehler: {exc}"
        return RedirectResponse(f"/system?msg={quote(msg)}", status_code=303)

    # ------------------------------------------------------------------ Aufbewahrung

    @app.get("/retention", response_class=HTMLResponse)
    def retention_page(request: Request, msg: str = "", session: Session = Depends(get_session)):
        rules = session.scalars(select(RetentionRule).order_by(RetentionRule.name)).all()
        log_entries = session.scalars(select(RetentionLog).order_by(RetentionLog.at.desc()).limit(50)).all()
        indexed = session.scalar(select(func.count()).select_from(Document))
        return render(
            request, "retention.html", rules=rules, overview=retention.overview(session, today()),
            log_entries=log_entries, msg=msg, indexed=indexed, settings=settings,
            last_index=pipeline.kv_get(session, "last_index_sync"),
            BASIS=retention.BASIS_LABELS, MATCH=retention.MATCH_LABELS,
        )

    @app.post("/retention/rules")
    def retention_rule_save(
        rule_id: int | None = Form(None), name: str = Form(...), match_type: str = Form(...),
        match_value: str = Form(...), years: int = Form(...), basis: str = Form("year_end"),
        session: Session = Depends(get_session),
    ):
        if match_type not in retention.MATCH_LABELS or basis not in retention.BASIS_LABELS or years < 0:
            raise HTTPException(400, "Ungültige Regel")
        rule = session.get(RetentionRule, rule_id) if rule_id else RetentionRule()
        if rule is None:
            raise HTTPException(404)
        rule.name, rule.match_type, rule.match_value = name.strip(), match_type, match_value.strip()
        rule.years, rule.basis = years, basis
        session.add(rule)
        return RedirectResponse("/retention", status_code=303)

    @app.post("/retention/rules/{rule_id}/delete")
    def retention_rule_delete(rule_id: int, session: Session = Depends(get_session)):
        if rule := session.get(RetentionRule, rule_id):
            session.delete(rule)
        return RedirectResponse("/retention", status_code=303)

    @app.post("/retention/index")
    def retention_index(session: Session = Depends(get_session)):
        paperless = make_paperless(settings, timeout=60)
        try:
            total, removed = pipeline.index_sync(session, paperless, settings)
            msg = f"Archiv-Index aktualisiert: {total} Dokumente, {removed} entfernt"
        except PaperlessError as exc:
            msg = f"Fehler: {exc}"
        finally:
            paperless.close()
        return RedirectResponse(f"/retention?msg={quote(msg)}", status_code=303)

    @app.post("/retention/apply")
    async def retention_apply(request: Request, session: Session = Depends(get_session)):
        form = await request.form()
        action = form.get("action")
        ids = [int(i) for i in form.getlist("doc") if str(i).isdigit()]
        if not ids or action not in ("tag", "delete", "hold"):
            return RedirectResponse(f"/retention?msg={quote('Keine Dokumente ausgewählt')}", status_code=303)
        if action == "delete" and (not settings.retention_delete_enabled or form.get("confirm") != "LÖSCHEN"):
            return RedirectResponse(
                f"/retention?msg={quote('Löschen nicht bestätigt (LÖSCHEN eintippen) oder deaktiviert')}", status_code=303
            )
        # Nur Dokumente verarbeiten, die laut aktueller Bewertung wirklich fällig sind (außer „behalten“)
        statuses = {s.doc.id: s for s in retention.overview(session, today())["due"]}
        if action != "hold":
            ids = [i for i in ids if i in statuses]
        paperless = make_paperless(settings, timeout=60)
        done, errors = 0, []
        try:
            if action == "tag" and ids:
                paperless.add_tag(ids, settings.retention_tag)
            for doc_id in ids:
                doc = session.get(Document, doc_id)
                if doc is None:
                    continue
                if action == "delete":
                    try:
                        paperless.delete_document(doc_id)
                    except PaperlessError as exc:
                        errors.append(str(exc))
                        continue
                status = statuses.get(doc_id)
                session.add(RetentionLog(
                    document_id=doc.id, title=doc.title, correspondent=doc.correspondent, created=doc.created,
                    action={"tag": "tagged", "delete": "deleted", "hold": "hold"}[action],
                    rule=status.rule.name if status and status.rule else None,
                ))
                if action == "hold":
                    doc.retention_hold = True
                elif action == "delete":
                    session.delete(doc)
                done += 1
        except PaperlessError as exc:
            errors.append(str(exc))
        finally:
            paperless.close()
        verb = {"tag": f"mit Tag „{settings.retention_tag}“ markiert", "delete": "gelöscht", "hold": "dauerhaft behalten"}[action]
        msg = f"{done} Dokumente {verb}" + (f". Fehler: {'; '.join(errors)}" if errors else "")
        return RedirectResponse(f"/retention?msg={quote(msg)}", status_code=303)

    @app.post("/documents/{doc_id}/hold")
    def document_hold(doc_id: int, hold: str = Form("1"), session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404)
        doc.retention_hold = hold == "1"
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    # ------------------------------------------------------------------ Vorgänge

    @app.get("/cases", response_class=HTMLResponse)
    def cases_page(request: Request, status: str = "open", session: Session = Depends(get_session)):
        query = select(Case).order_by(Case.follow_up.asc().nulls_last(), Case.created_at.desc())
        if status in ("open", "closed"):
            query = query.where(Case.status == status)
        return render(request, "cases.html", cases=session.scalars(query).all(), status=status)

    @app.post("/cases")
    def case_create(
        title: str = Form(...), document_id: str = Form(""), follow_up: str = Form(""),
        session: Session = Depends(get_session),
    ):
        case = Case(title=title.strip() or "Neuer Vorgang", follow_up=pipeline.coerce("date", follow_up))
        session.add(case)
        session.flush()
        if document_id.strip().isdigit():
            add_to_case(session, case, int(document_id))
        return RedirectResponse(f"/cases/{case.id}", status_code=303)

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    def case_detail(request: Request, case_id: int, q: str = "", msg: str = "", session: Session = Depends(get_session)):
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "Vorgang nicht gefunden")
        linked = {link.document_id for link in case.links}
        results = [d for d in cases.search_documents(session, q) if d.id not in linked] if q.strip() else []
        return render(
            request, "case.html", case=case, timeline=cases.timeline(case), q=q, results=results, msg=msg,
            link_suggestions=cases.link_suggestions(session, case), settings=settings,
        )

    @app.post("/cases/{case_id}/update")
    def case_update(
        case_id: int, title: str = Form(...), follow_up: str = Form(""), follow_up_note: str = Form(""),
        session: Session = Depends(get_session),
    ):
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(404)
        case.title = title.strip() or case.title
        case.follow_up = pipeline.coerce("date", follow_up)
        case.follow_up_note = follow_up_note.strip() or None
        return RedirectResponse(f"/cases/{case_id}", status_code=303)

    @app.post("/cases/{case_id}/status")
    def case_status(case_id: int, status: str = Form(...), session: Session = Depends(get_session)):
        case = session.get(Case, case_id)
        if case is None or status not in ("open", "closed"):
            raise HTTPException(404)
        case.status = status
        case.closed_at = datetime.now() if status == "closed" else None
        return RedirectResponse(f"/cases/{case_id}", status_code=303)

    @app.post("/cases/{case_id}/delete")
    def case_delete(case_id: int, session: Session = Depends(get_session)):
        if case := session.get(Case, case_id):
            session.delete(case)
        return RedirectResponse("/cases", status_code=303)

    @app.post("/cases/{case_id}/documents")
    def case_add_document(case_id: int, document_id: int = Form(...), back: str = Form(""), session: Session = Depends(get_session)):
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(404)
        msg = add_to_case(session, case, document_id)
        target = back if back.startswith("/") else f"/cases/{case_id}"
        return RedirectResponse(f"{target}{'&' if '?' in target else '?'}msg={quote(msg)}" if msg else target, status_code=303)

    @app.post("/documents/{doc_id}/add-to-case")
    def document_add_to_case(doc_id: int, case_id: int = Form(...), session: Session = Depends(get_session)):
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(404)
        add_to_case(session, case, doc_id)
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    @app.post("/cases/{case_id}/documents/{doc_id}/remove")
    def case_remove_document(case_id: int, doc_id: int, session: Session = Depends(get_session)):
        if link := session.get(CaseDocument, (case_id, doc_id)):
            session.delete(link)
        return RedirectResponse(f"/cases/{case_id}", status_code=303)

    @app.post("/cases/{case_id}/entries")
    def case_add_entry(case_id: int, text: str = Form(...), entry_date: str = Form(""), session: Session = Depends(get_session)):
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(404)
        if text.strip():
            session.add(CaseEntry(case_id=case_id, text=text.strip(), date=pipeline.coerce("date", entry_date) or today()))
        return RedirectResponse(f"/cases/{case_id}", status_code=303)

    @app.post("/cases/{case_id}/entries/{entry_id}/delete")
    def case_delete_entry(case_id: int, entry_id: int, session: Session = Depends(get_session)):
        entry = session.get(CaseEntry, entry_id)
        if entry is not None and entry.case_id == case_id:
            session.delete(entry)
        return RedirectResponse(f"/cases/{case_id}", status_code=303)

    def add_to_case(session: Session, case: Case, doc_id: int) -> str:
        paperless = make_paperless(settings, timeout=15)
        try:
            try:
                doc = cases.ensure_document(session, paperless, doc_id, settings)
            except PaperlessError as exc:
                return f"Dokument {doc_id} nicht gefunden: {exc}"
            if not cases.add_document(session, case, doc):
                return "Dokument ist bereits im Vorgang"
            if settings.cases_propose_links and settings.field_related:
                session.flush()
                session.refresh(case)
                cases.propose_links(case, doc)
            return ""
        finally:
            paperless.close()

    # ------------------------------------------------------------------ Für Paperless (nur mit Zustimmung)

    # ------------------------------------------------------------------ Belege (Buchungen ohne Beleg)

    @app.get("/receipts", response_class=HTMLResponse)
    def receipts_page(request: Request, state: str = "open", msg: str = "", session: Session = Depends(get_session)):
        query = select(ReceiptCheck).order_by(ReceiptCheck.tx_date.desc())
        if state == "open":
            query = query.where(ReceiptCheck.state.in_(("missing", "candidate")))
        elif state in receipts.STATES:
            query = query.where(ReceiptCheck.state == state)
        rows = session.scalars(query).all()
        titles = {d.id: d.title for d in session.scalars(select(Document).where(
            Document.id.in_([r.document_id for r in rows if r.document_id] or [-1])))}
        return render(request, "receipts.html", rows=rows, state=state, msg=msg, STATES=receipts.STATES,
                      overview=receipts.overview(session), ignored=receipts.ignored_counterparties(session),
                      titles=titles, settings=settings)

    @app.post("/receipts/run")
    def receipts_run(session: Session = Depends(get_session)):
        client = make_firefly(settings)
        if client is None:
            return RedirectResponse(f"/receipts?msg={quote('Firefly ist nicht eingerichtet (Einstellungen → Firefly III)')}", status_code=303)
        try:
            found = receipts.run(session, client, settings, today())
            msg = (f"{found['checked']} Abbuchungen geprüft: {found['missing']} ohne Beleg, "
                   f"{found['candidate']} mit möglichem Beleg, {found['linked']} zugeordnet")
        except FireflyError as exc:
            msg = f"Firefly nicht erreichbar: {exc}"
        finally:
            client.close()
        return RedirectResponse(f"/receipts?msg={quote(msg)}", status_code=303)

    @app.post("/receipts/{row_id}/{action}")
    def receipts_action(row_id: int, action: str, doc_id: str = Form(""), state: str = Form("open"),
                        session: Session = Depends(get_session)):
        row = session.get(ReceiptCheck, row_id)
        if row is None:
            raise HTTPException(404)
        if action == "link":
            ids = selection.parse_ids(doc_id)
            msg = "Beleg zugeordnet (nur im Companion)" if ids and receipts.link(session, row, ids[0]) \
                else "Dokument nicht gefunden – Paperless-ID prüfen (ggf. Archiv-Index neu einlesen)"
        elif action == "ignore":
            receipts.ignore(row)
            msg = "Als „kein Beleg nötig“ markiert"
        elif action == "ignore-counterparty":
            receipts.set_ignored_counterparties(session, [*receipts.ignored_counterparties(session), row.counterparty])
            for other in session.scalars(select(ReceiptCheck).where(ReceiptCheck.decided.is_(False))):
                if other.counterparty.lower() == row.counterparty.lower():
                    other.state, other.note = "ignored", "Empfänger ausgeblendet"
            msg = f"„{row.counterparty}“ wird künftig ausgeblendet"
        elif action == "reopen":
            receipts.reopen(row)
            msg = "Wieder offen"
        else:
            raise HTTPException(400)
        return RedirectResponse(f"/receipts?state={quote(state)}&msg={quote(msg)}", status_code=303)

    @app.post("/receipts-unignore")
    def receipts_unignore(name: str = Form(...), session: Session = Depends(get_session)):
        receipts.set_ignored_counterparties(session, [n for n in receipts.ignored_counterparties(session) if n != name])
        for row in session.scalars(select(ReceiptCheck).where(ReceiptCheck.decided.is_(False), ReceiptCheck.state == "ignored")):
            receipts.reopen(row)
        return RedirectResponse(f"/receipts?msg={quote(f'„{name}“ wird wieder geprüft (beim nächsten Lauf)')}", status_code=303)

    # ------------------------------------------------------------------ Regeln: Gelerntes und Vorschläge für Paperless

    @app.get("/rules", response_class=HTMLResponse)
    def rules_page(request: Request, msg: str = "", show: str = "open", session: Session = Depends(get_session)):
        rule_proposals.refresh(session)
        proposals = session.scalars(select(RuleProposal).order_by(RuleProposal.correspondent)).all()
        if show == "open":
            proposals = [p for p in proposals if p.state in ("proposed", "approved")]
        elif show in rule_proposals.STATES:
            proposals = [p for p in proposals if p.state == show]
        counts = Counter(p.state for p in session.scalars(select(RuleProposal)))
        ids, ids_error = None, None
        if any(p.state == "approved" for p in proposals):
            paperless = make_paperless(settings, timeout=15)
            try:
                ids = {e: {n.lower(): i for i, n in paperless.names(e).items()} for e in ("tags", "correspondents", "document_types")}
            except PaperlessError as exc:
                ids_error = str(exc)
            finally:
                paperless.close()
        tag_choices = sorted({t for tags in session.scalars(select(Document.tags)) for t in tags or []
                              if not pipeline.is_unresolved_tag(t)}, key=str.lower)
        doctype_choices = sorted({d for d in session.scalars(select(Document.document_type).distinct()) if d}, key=str.lower)
        return render(
            request, "rules.html", msg=msg, show=show, proposals=proposals, counts=counts, STATES=rule_proposals.STATES,
            preview=lambda p: rule_proposals.preview(session, p),
            payload=(lambda p: rule_proposals.api_payload(p, ids)) if ids else None, ids_error=ids_error,
            profiles=learning.profiles(session), accuracy=learning.accuracy(session), LEARN_FIELDS=learning.FIELDS,
            tag_choices=tag_choices, doctype_choices=doctype_choices, MIN_DOCS=rule_proposals.MIN_DOCS,
            SHARE=int(rule_proposals.SHARE * 100), SOURCE_LABELS=pipeline.SOURCE_LABELS,
        )

    @app.post("/rules/learned")
    def rules_learned(name: str = Form(...), attr: str = Form(...), disabled: str = Form("0"),
                      session: Session = Depends(get_session)):
        learning.set_disabled(session, name, attr, disabled == "1")
        msg = f"„{learning.FIELDS.get(attr, attr)}“ für {name} " + ("abgeschaltet" if disabled == "1" else "wieder aktiv")
        return RedirectResponse(f"/rules?msg={quote(msg)}#learned", status_code=303)

    @app.post("/rules/proposals/{proposal_id}")
    async def rules_proposal(proposal_id: int, request: Request, session: Session = Depends(get_session)):
        proposal = session.get(RuleProposal, proposal_id)
        if proposal is None:
            raise HTTPException(404)
        form = await request.form()
        action = form.get("action")
        if action == "save":
            rule_proposals.update(proposal, [str(t) for t in form.getlist("tag")], str(form.get("document_type") or ""))
            msg = "Regel geändert – Auswirkung unten neu berechnet"
        elif action in ("approved", "dismissed", "created", "proposed"):
            if action == "proposed":
                proposal.edited = False  # zurück auf den berechneten Vorschlag
            rule_proposals.set_state(proposal, action)
            msg = {"approved": "Freigegeben – unten steht, wie du die Regel in Paperless anlegst",
                   "dismissed": "Verworfen", "created": "Als in Paperless angelegt markiert",
                   "proposed": "Zurück auf Vorschlag"}[action]
        else:
            raise HTTPException(400)
        return RedirectResponse(f"/rules?msg={quote(msg)}#rule-{proposal_id}", status_code=303)

    @app.get("/paperless-changes", response_class=HTMLResponse)
    def paperless_changes(request: Request, msg: str = "", session: Session = Depends(get_session)):
        flt = {k: str(request.query_params.get(k) or "").strip() for k in metadata.FILTER_KEYS}
        what, source, value = flt["what"], flt["source"], flt["value"]
        doc_flt = {k: flt[k] for k in ("has_tag", "correspondent", "doctype", "q")}
        rows = pipeline.documents_with_changes(session, settings, today())
        names = fields.role_names(settings)
        meta_open, meta_accepted = [], []
        # Zähler für den Filter (über alle offenen Vorschläge, unabhängig vom gewählten Filter)
        counts = {"what": Counter(), "source": Counter(), "tags": Counter(),
                  "doc_tags": Counter(), "correspondents": Counter(), "doctypes": Counter()}
        for doc in session.scalars(select(Document).where(Document.meta_pending.is_(True)).order_by(Document.id)):
            items = metadata.pending(doc)
            if not items:
                doc.meta_pending = False
            if any(i["state"] == "open" for i in items):
                # Auswahllisten für die Dokument-Filter: jetziger Stand in Paperless
                counts["doc_tags"].update(doc.tags or [metadata.NONE])
                counts["correspondents"][doc.correspondent or metadata.NONE] += 1
                counts["doctypes"][doc.document_type or metadata.NONE] += 1
            if not metadata.doc_matches(doc, **doc_flt):
                continue
            for i in items:
                if i["state"] == "open":
                    counts["what"][metadata.item_type(i["key"])] += 1
                    counts["source"][i["source"]] += 1
                    if i["key"].startswith("tag:") and (not source or i["source"] == source):
                        counts["tags"][i["new"]] += 1
            items = metadata.filter_items(items, what, source, value)
            if accepted := [i for i in items if i["state"] == "accepted"]:
                meta_accepted.append((doc, accepted))
            if opened := [i for i in items if i["state"] == "open"]:
                meta_open.append((doc, opened))
        meta_filter = flt
        def newest(values):
            values = [v for v in values if v]
            return max(values) if values else None

        last = {
            # jüngster Wert unter den offenen Custom-Field-Vorschlägen
            "fields": newest(pipeline.change_info(d, role)["at"] for d, changes in rows for role, _, _ in changes),
            "analysis": session.scalar(select(func.max(Document.analyzed_at))),
            # jüngster offene Metadaten-Vorschlag und überhaupt zuletzt erstellter Vorschlag
            "meta_open": newest(i["at"] for _, items in meta_open for i in items),
            "meta_any": newest((s or {}).get("at") for s in session.scalars(
                select(Document.meta_suggestion).where(Document.meta_suggestion.is_not(None)))),
        }
        return render(
            request, "paperless_changes.html", rows=rows, last=last, change_info=pipeline.change_info,
            SOURCE_LABELS=pipeline.SOURCE_LABELS, names=names, msg=msg, create_fields=settings.writeback_create_fields,
            meta_open=meta_open, meta_accepted=meta_accepted, meta_filter=meta_filter, meta_counts=counts,
            mq=lambda **changes: meta_query({**flt, **changes}), NONE=metadata.NONE,
            meta_open_count=sum(len(i) for _, i in meta_open), meta_query=meta_query(meta_filter), meta_accepted_count=sum(len(i) for _, i in meta_accepted), quality=metadata.quality(session), META_ITEMS=metadata.ITEMS,
            META_SOURCES=metadata.SOURCES, hints=metadata.archive_hints(session), HINTS=metadata.HINTS,
            meta_queued=session.scalar(select(func.count()).select_from(Document).where(Document.meta_state.in_(("queued", "running")))),
            meta_status=pipeline.meta_overview(session), unresolved_tags=pipeline.unresolved_tag_count(session), queue_paused=pipeline.kv_get(session, "queue_paused") == "1",
            llm_now=settings.llm_allowed_at(datetime.now(ZoneInfo(settings.timezone)).hour),
            settings=settings,
        )

    @app.post("/paperless-changes/meta-backfill")
    def paperless_changes_meta_backfill(session: Session = Depends(get_session)):
        count = pipeline.queue_meta_for_analyzed(session)
        request_worker(app)
        msg = f"{count} bereits analysierte Dokumente für Metadaten-Vorschläge eingereiht"
        return RedirectResponse(f"/paperless-changes?msg={quote(msg)}#metadata", status_code=303)

    @app.post("/paperless-changes/meta")
    async def paperless_changes_meta(request: Request, session: Session = Depends(get_session)):
        form = await request.form()
        selected: dict[int, list[str]] = {}
        for item in form.getlist("item"):
            doc_id, _, key = str(item).partition(":")
            if doc_id.isdigit() and key:
                selected.setdefault(int(doc_id), []).append(key)
        action = str(form.get("action") or "")
        flt = {k: str(form.get(k) or "") for k in metadata.FILTER_KEYS}
        if action.endswith("_all"):
            # alle mit dem aktuellen Filter angezeigten (vorgemerkte bzw. offene)
            state = "accepted" if action == "transfer_all" else "open"
            selected = {doc.id: [i["key"] for i in items] for doc, items in metadata.pending_rows(session, state, **flt)}
            action = action.removesuffix("_all")
        if not any(selected.values()) or action not in ("accept", "dismiss", "withdraw", "transfer"):
            msg = "Nichts ausgewählt"
        else:
            msg = meta_action(session, action, selected)
        query = meta_query(flt)
        return RedirectResponse(f"/paperless-changes?{query}{'&' if query else ''}msg={quote(msg)}#metadata", status_code=303)

    def meta_query(flt: dict) -> str:
        from urllib.parse import urlencode

        return urlencode({k: v for k, v in flt.items() if v})

    @app.post("/paperless-changes")
    async def paperless_changes_apply(request: Request, session: Session = Depends(get_session)):
        form = await request.form()
        action = form.get("action")
        selected: dict[int, list[str]] = {}
        for item in form.getlist("change"):
            doc_id, _, role = str(item).partition(":")
            if doc_id.isdigit() and role in fields.ROLES:
                selected.setdefault(int(doc_id), []).append(role)
        if not selected or action not in ("approve", "dismiss"):
            return RedirectResponse(f"/paperless-changes?msg={quote('Nichts ausgewählt')}", status_code=303)
        done, errors = 0, []
        paperless = make_paperless(settings, timeout=30) if action == "approve" else None
        try:
            for doc_id, roles in selected.items():
                doc = session.get(Document, doc_id)
                if doc is None:
                    continue
                if action == "dismiss":
                    pipeline.dismiss_changes(doc, settings, roles, today())
                    done += len(roles)
                    continue
                try:
                    missing = pipeline.approve_changes(paperless, doc, settings, roles, today())
                    done += len(roles) - len(missing)
                    errors += [f"#{doc_id}: Feld „{fields.role_names(settings)[r]}“ fehlt in Paperless" for r in missing]
                except PaperlessError as exc:
                    errors.append(f"#{doc_id}: {exc}")
                session.flush()
        finally:
            if paperless:
                paperless.close()
        verb = "in Paperless übernommen" if action == "approve" else "verworfen"
        msg = f"{done} Änderungen {verb}" + (f". Probleme: {'; '.join(errors[:5])}" if errors else "")
        return RedirectResponse(f"/paperless-changes?msg={quote(msg)}", status_code=303)

    # ------------------------------------------------------------------ Steuer

    @app.get("/tax", response_class=HTMLResponse)
    def tax_page(request: Request, year: int | None = None, session: Session = Depends(get_session)):
        years = tax.available_years(session, settings)
        if year is None:
            year = today().year - 1 if (today().year - 1) in years or not years else years[0]
        items = tax.collect(session, year, settings)
        return render(
            request, "tax.html", year=year, years=sorted(set(years) | {year}, reverse=True),
            groups=tax.grouped(items), count=len(items), excluded=tax.excluded(session, settings), settings=settings,
        )

    @app.get("/tax/export")
    def tax_export(year: int, session: Session = Depends(get_session)):
        items = tax.collect(session, year, settings)
        if not items:
            raise HTTPException(404, f"Keine steuerrelevanten Dokumente für {year}")
        buffer = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024, dir=settings.data_dir)
        paperless = make_paperless(settings, timeout=120)
        try:
            tax.build_zip(items, year, paperless, buffer)
        finally:
            paperless.close()
        buffer.seek(0)
        return StreamingResponse(
            iter(lambda: buffer.read(64 * 1024), b""),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="Steuer_{year}.zip"'},
            background=BackgroundTask(buffer.close),
        )

    @app.post("/documents/{doc_id}/tax-year")
    def document_tax_year(doc_id: int, tax_year: str = Form(""), back: str = Form(""), session: Session = Depends(get_session)):
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404)
        doc.tax_year = int(tax_year) if tax_year.strip().isdigit() else None
        return RedirectResponse(back if back.startswith("/") else f"/documents/{doc_id}", status_code=303)

    # ------------------------------------------------------------------ API

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/webhook/paperless")
    async def webhook(request: Request, token: str | None = None, session: Session = Depends(get_session)):
        check_token(settings.webhook_token, token or request.headers.get("X-Companion-Token"))
        payload: dict = {}
        if "json" in request.headers.get("content-type", ""):
            payload = await request.json()
        else:
            payload = dict(await request.form()) or dict(request.query_params)
        doc_id = extract_doc_id(payload)
        if doc_id is None:
            # Ohne ID: einfach eine Synchronisation anstoßen
            app.state.worker.request_sync()
            return {"status": "sync_requested"}
        paperless = make_paperless(settings)
        try:
            pipeline.import_single(session, paperless, doc_id, settings)
        except PaperlessError as exc:
            raise HTTPException(502, str(exc)) from exc
        finally:
            paperless.close()
        request_worker(app)
        return {"status": "queued", "document": doc_id}

    @app.get("/calendar.ics")
    def calendar(token: str | None = None, session: Session = Depends(get_session)):
        check_token(settings.ics_token, token)
        body = ics.render(events.collect(session, today()), settings.paperless_link_base)
        return Response(body, media_type="text/calendar; charset=utf-8")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    return app


def ensure_writable(data_dir: Path) -> None:
    """Bricht mit einer verständlichen Meldung ab, wenn der Datenordner nicht beschreibbar ist."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".schreibtest"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Datenordner {data_dir} ist nicht beschreibbar ({exc.strerror}). "
            f"Der Companion läuft als UID {os.getuid()}. Auf dem Docker-Host z.B. "
            "`sudo chown -R 1000:1000 ./data` ausführen und den Container neu starten."
        ) from exc


def request_worker(app: FastAPI) -> None:
    worker = getattr(app.state, "worker", None)
    if worker is not None:
        worker.wake.set()


def extract_doc_id(payload: dict) -> int | None:
    for key in ("doc_id", "document_id", "id"):
        value = payload.get(key)
        if value is not None and str(value).isdigit():
            return int(value)
    for key in ("doc_url", "url", "document_url"):
        if value := payload.get(key):
            if m := re.search(r"/documents/(\d+)", str(value)):
                return int(m.group(1))
    return None


def asset_version() -> str:
    """Kurzer Hash über CSS/JS – ändert sich mit jedem Update und erzwingt so das Neuladen im Browser."""
    import hashlib

    digest = hashlib.sha1()
    for path in sorted((BASE / "static").glob("*.[cj]s*")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def field_badges(doc: Document, from_record: bool) -> dict:
    """Herkunft je Formularfeld: gespeicherte Herkunft bzw. die der Vorschläge."""
    sugg = {k: v for k, v in (doc.suggestions or {}).items() if isinstance(v, dict) and not k.startswith("_")}
    if not from_record:
        return {k: {"source": v.get("source"), "confidence": v.get("confidence"), "confirmed": False} for k, v in sugg.items()}
    badges = dict(doc.field_sources or {})
    if doc.review_state == "pending":
        # in der Inbox zählen aktuelle Vorschläge aus Firefly (Formular ist damit vorbelegt)
        badges.update({k: {**v, "confirmed": False} for k, v in sugg.items() if v.get("source") == "firefly"})
    return badges


def form_values(doc: Document) -> tuple[dict, bool]:
    """Formularwerte: gespeicherter Datensatz, sonst Vorschläge."""
    record = {"invoice": doc.invoice, "contract": doc.contract, "warranty": doc.warranty}.get(doc.kind)
    if record is None:
        return pipeline.suggestion_values(doc), False
    values = {name: getattr(record, name, None) for name in pipeline.FIELD_TYPES[doc.kind]}
    if doc.review_state == "pending":
        # Aus Firefly vorbelegte Werte auch dann zeigen, wenn es schon einen Datensatz gibt
        for name, item in (doc.suggestions or {}).items():
            if isinstance(item, dict) and item.get("source") == "firefly" and name in values:
                values[name] = pipeline.coerce(pipeline.FIELD_TYPES[doc.kind][name], item.get("value"))
    if doc.kind == "invoice":
        values["warranty_enabled"] = doc.warranty is not None
        values["warranty_months"] = doc.warranty.months if doc.warranty else None
    return values, True


app = create_app()
