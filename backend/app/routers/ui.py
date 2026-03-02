"""UI router for serving the transaction monitoring web interface"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings

router = APIRouter()


@router.get("/")
async def root():
    """Serve the real-time transaction monitoring UI"""
    network = settings.CARDANO_NETWORK
    # API key is intentionally NOT embedded in the HTML.
    # In dev mode (API_KEYS not set) all endpoints are open and the dashboard works
    # without a key.  In production, stats calls will return 403 and show "-"; operators
    # should access the dashboard from a trusted network or inject the key via a
    # reverse-proxy auth header.  Embedding the key in page source would expose it
    # to anyone with HTTP access to the host.

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

            /* Stats row */
            .stats {{
                display: grid;
                grid-template-columns: repeat(5, 1fr);
                gap: 12px;
                margin-bottom: 16px;
            }}
            .stat {{
                background: #1a1f3a;
                padding: 14px;
                border-radius: 8px;
                text-align: center;
            }}
            .stat-label {{ font-size: 11px; color: #888; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
            .stat-value {{ font-size: 22px; font-weight: 700; color: #00d4ff; }}
            .stat-value.pending {{ color: #ffaa00; }}
            .stat-value.confirmed {{ color: #00ff88; }}
            .stat-value.rollback {{ color: #ff4444; }}

            /* Panels */
            .panels {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 16px;
            }}
            .panel {{
                background: #1a1f3a;
                border-radius: 8px;
                overflow: hidden;
            }}
            .panel.full {{ grid-column: 1 / -1; }}
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
            .panel-header.block {{ background: #1a4d8f; color: #fff; }}
            .panel-header.mempool {{ background: #e65100; color: #fff; }}
            .panel-header.txs {{ background: #2e7d32; color: #fff; }}
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

            /* Block info */
            .block-info {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 8px;
            }}
            .block-field {{
                padding: 8px 12px;
                background: #151a35;
                border-radius: 4px;
            }}
            .block-field .label {{ font-size: 11px; color: #888; }}
            .block-field .value {{ font-size: 14px; color: #fff; font-family: 'Courier New', monospace; word-break: break-all; }}
            .block-field.wide {{ grid-column: 1 / -1; }}

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
            .tx-status.PENDING {{ background: #ffaa00; color: #000; }}
            .tx-status.CONFIRMED {{ background: #00ff88; color: #000; }}
            .tx-status.ROLLED_BACK {{ background: #ff4444; color: #fff; }}
            .tx-fee {{ color: #888; }}
            .tx-time {{ color: #666; }}
            .tx-outputs {{ color: #00ff88; font-weight: 600; }}

            .empty {{ text-align: center; padding: 30px; color: #555; font-size: 13px; }}

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

        <div class="stats">
            <div class="stat">
                <div class="stat-label">Total Tracked</div>
                <div class="stat-value" id="statTotal">-</div>
            </div>
            <div class="stat">
                <div class="stat-label">Pending</div>
                <div class="stat-value pending" id="statPending">-</div>
            </div>
            <div class="stat">
                <div class="stat-label">Confirmed</div>
                <div class="stat-value confirmed" id="statConfirmed">-</div>
            </div>
            <div class="stat">
                <div class="stat-label">Rolled Back</div>
                <div class="stat-value rollback" id="statRolledBack">-</div>
            </div>
            <div class="stat">
                <div class="stat-label">Avg Latency</div>
                <div class="stat-value" id="statLatency">-</div>
            </div>
        </div>

        <div class="panels">
            <!-- Latest Block -->
            <div class="panel">
                <div class="panel-header block">
                    Latest Block
                    <span class="panel-count" id="blockTxCount">-</span>
                </div>
                <div class="panel-body" id="blockPanel">
                    <div class="empty">Waiting for blocks...</div>
                </div>
            </div>

            <!-- Mempool -->
            <div class="panel">
                <div class="panel-header mempool">
                    Mempool
                    <span class="panel-count" id="mempoolCount">0</span>
                </div>
                <div class="panel-body" id="mempoolPanel">
                    <div class="empty">No pending transactions</div>
                </div>
            </div>

            <!-- Latest Transactions -->
            <div class="panel full">
                <div class="panel-header txs">
                    Latest Confirmed Transactions
                    <span class="panel-count" id="txsCount">0</span>
                </div>
                <div class="panel-body" id="txsPanel">
                    <div class="empty">Waiting for transactions...</div>
                </div>
            </div>
        </div>

        <script>
            // API_KEY is not embedded server-side.  Works out-of-the-box in dev
            // mode (API_KEYS not set).  In production, stats calls return 403 and
            // counters display "-" — configure auth at the reverse-proxy layer.
            const API_KEY = "";
            const headers = API_KEY ? {{"TMS-API-Key": API_KEY}} : {{}};

            // State
            let mempoolTxs = new Map();
            let confirmedTxs = [];
            let latestBlock = null;
            let txsCount = 0;

            // DOM refs
            const blockPanel = document.getElementById("blockPanel");
            const mempoolPanel = document.getElementById("mempoolPanel");
            const txsPanel = document.getElementById("txsPanel");
            const connStatus = document.getElementById("connStatus");

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
                if (ev.eventType === "TX_PENDING") {{
                    mempoolTxs.set(ev.txId, {{
                        txId: ev.txId,
                        firstSeenAt: ev.firstSeenAt || ev.observedAt,
                    }});
                    renderMempool();
                }} else if (ev.eventType === "TX_CONFIRMED") {{
                    // Remove from mempool
                    mempoolTxs.delete(ev.txId);
                    renderMempool();

                    // Add to confirmed list
                    confirmedTxs.unshift({{
                        txId: ev.txId,
                        observedAt: ev.observedAt,
                        block: ev.block || {{}},
                    }});
                    if (confirmedTxs.length > 100) confirmedTxs.pop();
                    txsCount++;
                    renderConfirmed();

                    // Update block panel
                    if (ev.block) {{
                        if (!latestBlock || ev.block.slot > latestBlock.slot) {{
                            latestBlock = {{ ...ev.block, txCount: 1, time: ev.observedAt }};
                        }} else if (ev.block.slot === latestBlock.slot) {{
                            latestBlock.txCount++;
                        }}
                        renderBlock();
                    }}
                }} else if (ev.eventType === "TX_ROLLED_BACK") {{
                    // Visual indicator — could flash or mark in UI
                }}
                refreshStats();
            }}

            // --- Rendering ---
            function renderBlock() {{
                if (!latestBlock) return;
                const b = latestBlock;
                blockPanel.innerHTML = `
                    <div class="block-info">
                        <div class="block-field">
                            <div class="label">Height</div>
                            <div class="value">${{b.height || '-'}}</div>
                        </div>
                        <div class="block-field">
                            <div class="label">Slot</div>
                            <div class="value">${{b.slot || '-'}}</div>
                        </div>
                        <div class="block-field wide">
                            <div class="label">Hash</div>
                            <div class="value">${{b.hash || '-'}}</div>
                        </div>
                        <div class="block-field">
                            <div class="label">Transactions</div>
                            <div class="value">${{b.txCount || 0}}</div>
                        </div>
                        <div class="block-field">
                            <div class="label">Time</div>
                            <div class="value">${{b.time ? new Date(b.time).toLocaleTimeString() : '-'}}</div>
                        </div>
                    </div>
                `;
                document.getElementById("blockTxCount").textContent = (b.txCount || 0) + " txs";
            }}

            function renderMempool() {{
                const items = Array.from(mempoolTxs.values());
                document.getElementById("mempoolCount").textContent = items.length;
                if (items.length === 0) {{
                    mempoolPanel.innerHTML = '<div class="empty">No pending transactions</div>';
                    return;
                }}
                mempoolPanel.innerHTML = items.map(tx => `
                    <div class="tx-row">
                        <span class="tx-hash">${{tx.txId.substring(0, 16)}}...${{tx.txId.substring(tx.txId.length - 8)}}</span>
                        <div class="tx-meta">
                            <span class="tx-status PENDING">PENDING</span>
                            <span class="tx-time">${{new Date(tx.firstSeenAt).toLocaleTimeString()}}</span>
                        </div>
                    </div>
                `).join('');
            }}

            function renderConfirmed() {{
                document.getElementById("txsCount").textContent = txsCount;
                if (confirmedTxs.length === 0) {{
                    txsPanel.innerHTML = '<div class="empty">Waiting for transactions...</div>';
                    return;
                }}
                txsPanel.innerHTML = confirmedTxs.map(tx => `
                    <div class="tx-row">
                        <span class="tx-hash">${{tx.txId.substring(0, 20)}}...${{tx.txId.substring(tx.txId.length - 10)}}</span>
                        <div class="tx-meta">
                            <span class="tx-status CONFIRMED">CONFIRMED</span>
                            ${{tx.block.height ? `<span class="tx-fee">Block ${{tx.block.height}}</span>` : ''}}
                            <span class="tx-time">${{new Date(tx.observedAt).toLocaleTimeString()}}</span>
                        </div>
                    </div>
                `).join('');
            }}

            // --- Stats polling ---
            async function refreshStats() {{
                try {{
                    const res = await fetch("/api/lifecycle/stats/summary", {{ headers }});
                    const s = await res.json();
                    document.getElementById("statTotal").textContent = s.total_tracked;
                    document.getElementById("statPending").textContent = s.pending_count;
                    document.getElementById("statConfirmed").textContent = s.confirmed_count;
                    document.getElementById("statRolledBack").textContent = s.rolled_back_count;
                    document.getElementById("statLatency").textContent =
                        s.avg_latency_ms != null ? (s.avg_latency_ms / 1000).toFixed(1) + "s" : "-";
                }} catch(e) {{}}
            }}

            // Initial load
            refreshStats();
            setInterval(refreshStats, 15000);

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
                        // Set latest block from most recent tx
                        const latest = data.data[0];
                        if (latest.slot) {{
                            latestBlock = {{
                                hash: latest.block_hash,
                                slot: latest.slot,
                                height: latest.height,
                                txCount: data.data.filter(t => t.slot === latest.slot).length,
                                time: latest.confirmed_at,
                            }};
                            renderBlock();
                        }}
                    }}
                }} catch(e) {{}}
            }})();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
