"""UI router for serving the transaction monitoring web interface"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings

router = APIRouter()


@router.get("/")
async def root():
    """Serve the real-time transaction monitoring UI"""
    network = settings.CARDANO_NETWORK

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Cardano TMS — {network.capitalize()}</title>
        <meta charset="utf-8">
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #0a0e27;
                color: #e0e0e0;
                padding: 16px;
            }}
            .header {{
                background: #1a1f3a;
                padding: 16px 20px;
                border-radius: 8px;
                margin-bottom: 16px;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}
            .header h1 {{
                color: #00d4ff;
                font-size: 18px;
            }}
            .header-right {{
                display: flex;
                gap: 12px;
                align-items: center;
            }}
            .badge {{
                padding: 4px 12px;
                border-radius: 12px;
                font-size: 12px;
                font-weight: 600;
            }}
            .badge.connected {{ background: #00ff88; color: #000; }}
            .badge.disconnected {{ background: #ff4444; color: #fff; }}
            .badge.network {{ background: #6b4c9a; color: #fff; }}

            /* Panels */
            .panels {{
                display: grid;
                grid-template-columns: 1fr;
                gap: 16px;
            }}
            .panel {{
                background: #1a1f3a;
                border-radius: 8px;
                overflow: hidden;
            }}
            .panel-header {{
                padding: 12px 16px;
                font-size: 13px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}
            .panel-header.txs {{ background: #2e7d32; color: #fff; }}
            .panel-header.risk {{ background: #b71c1c; color: #fff; }}
            .panel-body {{
                padding: 12px;
                max-height: 45vh;
                overflow-y: auto;
            }}
            .panel-count {{
                background: rgba(255,255,255,0.2);
                padding: 2px 10px;
                border-radius: 10px;
                font-size: 12px;
            }}

            /* Transaction rows */
            .tx-row {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 10px 12px;
                background: #151a35;
                border-radius: 4px;
                margin-bottom: 6px;
                transition: background 0.2s;
            }}
            .tx-row:hover {{ background: #1d2340; }}
            .tx-hash {{
                font-family: 'Courier New', monospace;
                font-size: 13px;
                color: #00d4ff;
            }}
            .tx-meta {{
                display: flex;
                gap: 12px;
                align-items: center;
                font-size: 12px;
            }}
            .tx-status {{
                padding: 2px 8px;
                border-radius: 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            .tx-status.CONFIRMED {{ background: #00ff88; color: #000; }}
            .tx-fee {{ color: #888; }}
            .tx-time {{ color: #666; }}

            .empty {{ text-align: center; padding: 30px; color: #555; font-size: 13px; }}
            .copy-btn {{
                background: none; border: 1px solid #3a3f5a; color: #888;
                border-radius: 4px; padding: 2px 6px; font-size: 11px;
                cursor: pointer; margin-left: 6px; transition: all 0.15s;
            }}
            .copy-btn:hover {{ border-color: #00d4ff; color: #00d4ff; }}
            .copy-btn.copied {{ border-color: #00ff88; color: #00ff88; }}
            .archive-btn {{
                background: none; border: 1px solid #3a3f5a; color: #888;
                border-radius: 4px; padding: 2px 8px; font-size: 11px;
                cursor: pointer; margin-left: 6px; transition: all 0.15s;
            }}
            .archive-btn:hover {{ border-color: #ffab00; color: #ffab00; }}
            .archive-btn.restore {{ }}
            .archive-btn.restore:hover {{ border-color: #00c853; color: #00c853; }}
            .archive-note {{
                color: #888; font-size: 11px; margin-top: 4px;
                font-style: italic;
            }}
            .panel-header.archive {{ background: #4527a0; color: #fff; }}

            /* Risk bands */
            .risk-band {{
                padding: 2px 8px;
                border-radius: 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            .risk-band.Critical {{ background: #ff1744; color: #fff; }}
            .risk-band.High {{ background: #ff6d00; color: #fff; }}
            .risk-band.Moderate {{ background: #ffab00; color: #000; }}
            .risk-band.Informational {{ background: #00c853; color: #000; }}
            .attack-class {{ color: #ce93d8; font-size: 12px; font-weight: 600; }}
            .filter-label {{ color: #888; font-size: 11px; margin-right: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
            .band-swatch {{
                display: inline-block;
                width: 8px;
                height: 8px;
                border-radius: 50%;
                margin-right: 6px;
                vertical-align: middle;
            }}
            .class-score {{
                display: inline-block;
                padding: 1px 6px;
                border-radius: 8px;
                font-size: 10px;
                font-weight: 700;
                margin-left: 4px;
                vertical-align: middle;
            }}
            .risk-score {{ color: #fff; font-size: 13px; font-weight: 700; }}
            .risk-sub {{ font-size: 11px; color: #888; margin-top: 2px; }}
            .risk-row {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 10px 12px;
                background: #151a35;
                border-radius: 4px;
                margin-bottom: 6px;
                border-left: 3px solid transparent;
            }}
            .risk-row.Critical {{ border-left-color: #ff1744; }}
            .risk-row.High {{ border-left-color: #ff6d00; }}
            .risk-row.Moderate {{ border-left-color: #ffab00; }}
            .risk-row.Informational {{ border-left-color: #00c853; }}

            /* Filters */
            .filters {{
                display: flex;
                gap: 8px;
                padding: 10px 12px;
                flex-wrap: wrap;
                border-bottom: 1px solid #2a2f4a;
            }}
            .filter-btn {{
                padding: 4px 12px;
                border-radius: 14px;
                font-size: 11px;
                font-weight: 600;
                border: 1px solid #3a3f5a;
                background: transparent;
                color: #888;
                cursor: pointer;
                transition: all 0.15s;
            }}
            .filter-btn:hover {{ border-color: #00d4ff; color: #00d4ff; }}
            .filter-btn.active {{ background: #00d4ff; color: #000; border-color: #00d4ff; }}
            .risk-details {{ display: flex; gap: 10px; align-items: center; }}
            .score-bar {{ width: 60px; height: 6px; background: #2a2f4a; border-radius: 3px; overflow: hidden; }}
            .score-bar-fill {{ height: 100%; border-radius: 3px; }}
            .score-bar-fill.Critical {{ background: #ff1744; }}
            .score-bar-fill.High {{ background: #ff6d00; }}
            .score-bar-fill.Moderate {{ background: #ffab00; }}
            .score-bar-fill.Informational {{ background: #00c853; }}

            /* Scrollbar */
            ::-webkit-scrollbar {{ width: 6px; }}
            ::-webkit-scrollbar-track {{ background: transparent; }}
            ::-webkit-scrollbar-thumb {{ background: #2a2f4a; border-radius: 3px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Cardano Transaction Monitoring System</h1>
            <div class="header-right">
                <span class="badge network">{network.capitalize()}</span>
                <span class="badge disconnected" id="connStatus">Disconnected</span>
            </div>
        </div>

        <div class="panels">
            <!-- Risk Alerts -->
            <div class="panel">
                <div class="panel-header risk">
                    Risk Alerts
                    <span class="panel-count" id="riskCount">0</span>
                </div>
                <div class="filters" id="riskFilters">
                    <span class="filter-label">Show:</span>
                    <button class="filter-btn band-btn active" data-band="80">
                        <span class="band-swatch" style="background:#ff1744"></span>Critical only
                    </button>
                    <button class="filter-btn band-btn" data-band="60">
                        <span class="band-swatch" style="background:#ff6d00"></span>High and above
                    </button>
                    <button class="filter-btn band-btn" data-band="31">
                        <span class="band-swatch" style="background:#ffab00"></span>Moderate and above
                    </button>
                    <button class="filter-btn band-btn" data-band="1">
                        <span class="band-swatch" style="background:#00c853"></span>All (inc. Informational)
                    </button>
                    <span style="border-left:1px solid #3a3f5a;height:20px;margin:0 4px"></span>
                    <button class="filter-btn active" data-attack="">All classes</button>
                    <button class="filter-btn" data-attack="token_dust">Token Dust</button>
                    <button class="filter-btn" data-attack="large_value">Large Value</button>
                    <button class="filter-btn" data-attack="large_datum">Large Datum</button>
                    <button class="filter-btn" data-attack="multiple_sat">Multiple Sat</button>
                    <button class="filter-btn" data-attack="front_running">Front-Running</button>
                    <button class="filter-btn" data-attack="sandwich">Sandwich</button>
                    <button class="filter-btn" data-attack="circular">Circular</button>
                    <button class="filter-btn" data-attack="fake_token">Fake Token</button>
                    <button class="filter-btn" data-attack="phishing">Phishing</button>
                    <span style="border-left:1px solid #3a3f5a;height:20px;margin:0 4px"></span>
                    <button class="filter-btn" data-sort="score">By Score</button>
                    <button class="filter-btn active" data-sort="date">By Date</button>
                </div>
                <div class="panel-body" id="riskPanel">
                    <div class="empty">No risky transactions detected</div>
                </div>
            </div>

            <!-- Latest Transactions -->
            <div class="panel">
                <div class="panel-header txs">
                    Latest Confirmed Transactions
                    <span class="panel-count" id="txsCount">0</span>
                </div>
                <div class="panel-body" id="txsPanel">
                    <div class="empty">Waiting for transactions...</div>
                </div>
            </div>

            <!-- Archived Alerts -->
            <div class="panel">
                <div class="panel-header archive">
                    Archived (False Positives)
                    <span class="panel-count" id="archiveCount">0</span>
                </div>
                <div class="panel-body" id="archivePanel">
                    <div class="empty">No archived alerts</div>
                </div>
            </div>
        </div>

        <script>
            const API_KEY = "";
            const headers = API_KEY ? {{"TMS-API-Key": API_KEY}} : {{}};

            // HTML-escape for every server/chain-derived value rendered via
            // innerHTML. Quotes included so attribute contexts (onclick
            // arguments) cannot be broken out of; & first so entities are
            // not double-escaped. Stored XSS via archive source_label was a
            // review finding — never interpolate a raw field.
            const esc = (v) => String(v ?? '').replace(/[&<>"']/g,
                c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);

            // State
            let confirmedTxs = [];
            let txsCount = 0;

            // DOM refs
            const txsPanel = document.getElementById("txsPanel");
            const connStatus = document.getElementById("connStatus");

            function copyTx(btn, hash) {{
                navigator.clipboard.writeText(hash).then(() => {{
                    btn.textContent = "Copied";
                    btn.classList.add("copied");
                    setTimeout(() => {{ btn.textContent = "Copy"; btn.classList.remove("copied"); }}, 1500);
                }});
            }}

            // --- WebSocket ---
            const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
            const ws = new WebSocket(protocol + "//" + window.location.host + "/ws");

            ws.onopen = () => {{
                connStatus.textContent = "Connected";
                connStatus.className = "badge connected";
            }};
            ws.onclose = () => {{
                connStatus.textContent = "Disconnected";
                connStatus.className = "badge disconnected";
            }};
            ws.onerror = () => {{}};

            ws.onmessage = (event) => {{
                const msg = JSON.parse(event.data);
                if (msg.type === "lifecycle") {{
                    handleLifecycleEvent(msg.data);
                }}
            }};

            function handleLifecycleEvent(ev) {{
                if (ev.eventType === "TX_CONFIRMED") {{
                    confirmedTxs.unshift({{
                        txId: ev.txId,
                        observedAt: ev.observedAt,
                        block: ev.block || {{}},
                    }});
                    if (confirmedTxs.length > 100) confirmedTxs.pop();
                    txsCount++;
                    renderConfirmed();
                    debouncedRefreshRisk();
                }}
            }}

            // --- Rendering ---
            function renderConfirmed() {{
                document.getElementById("txsCount").textContent = txsCount;
                if (confirmedTxs.length === 0) {{
                    txsPanel.innerHTML = '<div class="empty">Waiting for transactions...</div>';
                    return;
                }}
                txsPanel.innerHTML = confirmedTxs.map(tx => `
                    <div class="tx-row">
                        <span class="tx-hash">${{esc(tx.txId.substring(0, 20))}}...${{esc(tx.txId.substring(tx.txId.length - 10))}}</span><button class="copy-btn" onclick="copyTx(this,'${{esc(tx.txId)}}')">Copy</button>
                        <div class="tx-meta">
                            <span class="tx-status CONFIRMED">CONFIRMED</span>
                            ${{tx.block.height ? `<span class="tx-fee">Block ${{esc(tx.block.height)}}</span>` : ''}}
                            <span class="tx-time">${{new Date(tx.observedAt).toLocaleTimeString()}}</span>
                        </div>
                    </div>
                `).join('');
            }}

            // --- Risk Alerts ---
            const riskPanel = document.getElementById("riskPanel");
            const CLASS_LABELS = {{
                token_dust: "Token Dust",
                large_value: "Large Value",
                large_datum: "Large Datum",
                multiple_sat: "Multiple Satisfaction",
                front_running: "Front-Running",
                sandwich: "Sandwich",
                circular: "Circular Transfer",
                fake_token: "Fake Token",
                phishing: "Phishing",
            }};

            const SUB_SCORE_LABELS = {{
                value_cbor_bytes: "Large CBOR payload",
                unique_assetclass_count: "Many distinct tokens",
                lovelace_inverted: "Low ADA amount",
                sender_recurrence: "Repeated sender",
                quantity_digits: "Extreme token quantity",
                datum_bytes: "Large datum size",
                datum_ratio: "High datum-to-value ratio",
                value_cbor_bytes_inverted: "Small value payload",
                s_extraction: "Value extracted from script",
                s_exunits_inv: "Low execution units per input",
                s_inputs: "Many inputs from same script",
                s_recurrence: "Repeated sender on script",
                collision_outcome: "Collision detected",
                mempool_delta_inv: "Fast mempool submission",
                attacker_recurrence: "Repeated attacker",
                structural_similarity: "Structurally similar txs",
                attacker_link: "Linked attacker addresses",
                swap_rate_delta: "DEX rate manipulation",
                price_impact: "Price impact detected",
                profit: "Profit extracted",
                recurrence: "Repeated pattern",
                amount_similarity: "Similar amounts in cycle",
                cycle_recurrence: "Repeated cycle",
                recipient_entropy_inv: "Low recipient diversity",
                speed: "Fast cycle completion",
                tokenname_similarity: "Similar token name",
                unicode_suspicion: "Suspicious unicode",
                cip25_similarity: "Similar CIP-25 metadata",
                recipient_count: "Many recipients",
                mint_ratio_inv: "Low mint ratio",
                policy_age_inv: "New policy",
                url_recurrence: "Recurring phishing URL",
                targeting: "Targeted delivery",
                sender_recurrence_phish: "Repeated phishing sender",
            }};

            function explainSubScores(sub, cls) {{
                const entries = Object.entries(sub[cls] || {{}});
                if (entries.length === 0) return "";
                const top = entries
                    .filter(([_, v]) => typeof v === "number" && v > 0.3 && v <= 1.0)
                    .sort((a, b) => b[1] - a[1])
                    .slice(0, 3);
                if (top.length === 0) return "";
                return top.map(([k, v]) =>
                    `<span style="color:#aaa">${{SUB_SCORE_LABELS[k] || esc(k)}}</span> <span style="color:${{v > 0.7 ? '#ff6d00' : '#888'}}">${{(v * 100).toFixed(0)}}%</span>`
                ).join(' &middot; ');
            }}

            function renderRiskAlerts(alerts) {{
                document.getElementById("riskCount").textContent = alerts.length;
                if (alerts.length === 0) {{
                    riskPanel.innerHTML = '<div class="empty">No risky transactions detected</div>';
                    return;
                }}
                const bandOf = (s) => {{
                    if (s >= 80) return 'Critical';
                    if (s >= 60) return 'High';
                    if (s >= 31) return 'Moderate';
                    return 'Informational';
                }};
                riskPanel.innerHTML = alerts.map(a => {{
                    const topClasses = Object.entries(a.scores)
                        .filter(([_, s]) => s > 0)
                        .sort((x, y) => y[1] - x[1])
                        .slice(0, 3);
                    const classHtml = topClasses.map(([cls, score]) =>
                        `<span class="attack-class">${{CLASS_LABELS[cls] || esc(cls)}}</span>
                         <span class="class-score risk-band ${{bandOf(score)}}">${{score.toFixed(1)}}</span>`
                    ).join(' &middot; ');

                    // Sub-score explanation for the top class
                    const explain = a.sub_scores ? explainSubScores(a.sub_scores, a.max_class) : "";

                    const fee = a.fee != null ? (a.fee / 1_000_000).toFixed(3) + " ADA" : "-";
                    const outs = a.output_count != null ? a.output_count : "-";
                    const when = a.analyzed_at ? new Date(a.analyzed_at).toLocaleString() : "-";
                    return `
                        <div class="risk-row ${{esc(a.risk_band)}}">
                            <div style="flex:1;min-width:0">
                                <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
                                    <span class="tx-hash">${{esc(a.tx_hash.substring(0, 16))}}...${{esc(a.tx_hash.substring(a.tx_hash.length - 8))}}</span><button class="copy-btn" onclick="copyTx(this,'${{esc(a.tx_hash)}}')">Copy</button><button class="archive-btn" onclick="archiveAlert('${{esc(a.tx_hash)}}','${{esc(a.network)}}')">Archive</button>
                                    <span style="color:#888;font-size:11px">Fee: ${{fee}}</span>
                                    <span style="color:#888;font-size:11px">Outputs: ${{outs}}</span>
                                    <span style="color:#666;font-size:11px">${{when}}</span>
                                </div>
                                <div class="risk-sub">${{classHtml}}</div>
                                ${{explain ? `<div class="risk-sub" style="margin-top:2px">${{explain}}</div>` : ''}}
                            </div>
                            <div class="risk-details">
                                <div class="score-bar">
                                    <div class="score-bar-fill ${{esc(a.risk_band)}}" style="width:${{esc(a.max_score)}}%"></div>
                                </div>
                                <span class="risk-score">${{a.max_score.toFixed(0)}}</span>
                                <span class="risk-band ${{esc(a.risk_band)}}">${{esc(a.risk_band)}}</span>
                            </div>
                        </div>
                    `;
                }}).join('');
            }}

            let activeClassFilter = "";
            let activeSort = "date";
            // Default: Critical band only (score >= 80). Band-min-score map
            // mirrors normalise.score_to_band: Critical=80, High=60, Moderate=31.
            let activeMinScore = 80;
            let _riskTimer = null;
            function debouncedRefreshRisk() {{
                if (_riskTimer) return;
                _riskTimer = setTimeout(() => {{ _riskTimer = null; refreshRiskAlerts(); }}, 5000);
            }}

            async function refreshRiskAlerts() {{
                try {{
                    let url = `/api/analysis/results?min_score=${{activeMinScore}}&limit=50&sort=${{activeSort}}`;
                    if (activeClassFilter) {{
                        url += "&attack_class=" + activeClassFilter;
                    }}
                    const res = await fetch(url, {{ headers }});
                    if (!res.ok) return;
                    const data = await res.json();
                    renderRiskAlerts(data.data || []);
                }} catch(e) {{}}
            }}

            // Filter + sort buttons (three orthogonal axes: band / class / sort)
            document.getElementById("riskFilters").addEventListener("click", (e) => {{
                const btn = e.target.closest(".filter-btn");
                if (!btn) return;
                if (btn.dataset.band !== undefined) {{
                    document.querySelectorAll("#riskFilters [data-band]").forEach(b => b.classList.remove("active"));
                    btn.classList.add("active");
                    activeMinScore = parseFloat(btn.dataset.band);
                }} else if (btn.dataset.sort) {{
                    document.querySelectorAll("#riskFilters [data-sort]").forEach(b => b.classList.remove("active"));
                    btn.classList.add("active");
                    activeSort = btn.dataset.sort;
                }} else {{
                    document.querySelectorAll("#riskFilters [data-attack]").forEach(b => b.classList.remove("active"));
                    btn.classList.add("active");
                    activeClassFilter = btn.dataset.attack || "";
                }}
                refreshRiskAlerts();
            }});

            // --- Archive (false-positive curation) ---
            // Persist the admin label between archive actions so the user
            // doesn't get prompted for it every time. Keep it in localStorage
            // so it survives reloads.
            function getArchivedBy() {{
                let v = localStorage.getItem("tms_archived_by");
                if (!v) {{
                    v = prompt("Your name / email (used as archived_by). Saved locally.") || "";
                    if (v) localStorage.setItem("tms_archived_by", v);
                }}
                return v;
            }}

            async function archiveAlert(txHash, network) {{
                const archivedBy = getArchivedBy();
                if (!archivedBy) return;
                const note = prompt("Why is this not actually dangerous?");
                if (note === null) return;       // user cancelled
                if (!note.trim()) {{ alert("A note is required."); return; }}
                try {{
                    const res = await fetch("/api/archive", {{
                        method: "POST",
                        headers: {{ "Content-Type": "application/json", ...headers }},
                        body: JSON.stringify({{
                            network, tx_hash: txHash,
                            note: note.trim(), archived_by: archivedBy,
                        }}),
                    }});
                    if (!res.ok) {{
                        const body = await res.text();
                        alert("Archive failed: " + res.status + " " + body);
                        return;
                    }}
                    // Refresh both panels: the row leaves risk (server-side
                    // filter) and appears in archive.
                    refreshRiskAlerts();
                    refreshArchive();
                }} catch(e) {{ alert("Archive failed: " + e); }}
            }}

            async function restoreAlert(txHash, network) {{
                if (!confirm("Restore this transaction? It will reappear in the dangerous list.")) return;
                try {{
                    const res = await fetch(
                        `/api/archive/${{encodeURIComponent(txHash)}}?network=${{encodeURIComponent(network)}}`,
                        {{ method: "DELETE", headers }},
                    );
                    if (!res.ok && res.status !== 204) {{
                        const body = await res.text();
                        alert("Restore failed: " + res.status + " " + body);
                        return;
                    }}
                    refreshArchive();
                    refreshRiskAlerts();
                }} catch(e) {{ alert("Restore failed: " + e); }}
            }}

            function renderArchive(items) {{
                const panel = document.getElementById("archivePanel");
                document.getElementById("archiveCount").textContent = items.length;
                if (!items.length) {{
                    panel.innerHTML = '<div class="empty">No archived alerts</div>';
                    return;
                }}
                panel.innerHTML = items.map(a => {{
                    const when = a.archived_at ? new Date(a.archived_at).toLocaleString() : "-";
                    const wasClass = a.max_class || "-";
                    const wasBand = a.risk_band || "-";
                    const wasScore = a.max_score != null ? a.max_score.toFixed(0) : "-";
                    const noteHtml = a.note ? `<div class="archive-note">"${{esc(a.note)}}"</div>` : '';
                    return `
                        <div class="risk-row" style="border-left-color:#4527a0">
                            <div style="flex:1;min-width:0">
                                <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
                                    <span class="tx-hash">${{esc(a.tx_hash.substring(0, 16))}}...${{esc(a.tx_hash.substring(a.tx_hash.length - 8))}}</span><button class="copy-btn" onclick="copyTx(this,'${{esc(a.tx_hash)}}')">Copy</button><button class="archive-btn restore" onclick="restoreAlert('${{esc(a.tx_hash)}}','${{esc(a.network)}}')">Restore</button>
                                    <span style="color:#888;font-size:11px">By: ${{esc(a.archived_by || '-')}}</span>
                                    <span style="color:#666;font-size:11px">${{when}}</span>
                                </div>
                                <div class="risk-sub">Was: ${{esc(wasClass)}} (${{esc(wasBand)}}, ${{esc(wasScore)}}) &middot; source: ${{esc(a.source || 'local')}}</div>
                                ${{noteHtml}}
                            </div>
                        </div>
                    `;
                }}).join('');
            }}

            async function refreshArchive() {{
                try {{
                    const res = await fetch("/api/archive?limit=100", {{ headers }});
                    if (!res.ok) return;
                    const data = await res.json();
                    renderArchive(data.data || []);
                }} catch(e) {{}}
            }}

            // Initial load
            refreshRiskAlerts();
            refreshArchive();
            setInterval(refreshRiskAlerts, 20000);
            setInterval(refreshArchive, 30000);

            // Load recent confirmed txs on page load
            (async () => {{
                try {{
                    const res = await fetch("/api/lifecycle?status=CONFIRMED&limit=20", {{ headers }});
                    const data = await res.json();
                    if (data.data && data.data.length > 0) {{
                        confirmedTxs = data.data.map(r => ({{
                            txId: r.tx_id,
                            observedAt: r.confirmed_at || r.created_at,
                            block: {{ hash: r.block_hash, slot: r.slot, height: r.height }},
                        }}));
                        txsCount = confirmedTxs.length;
                        renderConfirmed();
                    }}
                }} catch(e) {{}}
            }})();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
