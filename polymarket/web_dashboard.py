from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from polymarket.config import LiveTradingConfig, database_url_from_env, live_trading_config_from_env
from polymarket.execution import OrderIntent, build_trading_client, submit_limit
from polymarket.gamma import GammaClient
from polymarket.logging_utils import init_trade_loggers, log_json
from polymarket.market_utils import outcome_legs
from polymarket.runtime_config import load_runtime_config, save_runtime_config
from polymarket.storage import TradingStorage
from polymarket.clob_public import ClobPublicClient


def _read_tail(path: Path, max_lines: int = 300) -> str:
    if not path.exists():
        return "(日志文件不存在)"
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


app = FastAPI(title="AI Polymarket Dashboard")
LOGGERS = init_trade_loggers("logs")


class ManualOrderRequest(BaseModel):
    slug: str
    outcome_index: int = 0
    side: str = "BUY"
    price: float
    size: float
    signature_type: int | None = None
    confirm_live: str = "NO"


class RuntimeConfigRequest(BaseModel):
    live_mode: bool
    top_markets: int
    max_orders: int
    min_confidence: float
    default_size: float
    analysis_timeout_s: float
    interval_seconds: int
    signature_type: int


class ClosePositionRequest(BaseModel):
    token_id: str
    market_slug: str
    size: float | None = None
    signature_type: int | None = None
    confirm_live: str = "NO"


@app.get("/api/positions")
def api_positions():
    storage = TradingStorage(database_url_from_env())
    return JSONResponse(content=jsonable_encoder(storage.fetch_latest_positions()))


@app.get("/api/trades")
def api_trades(days: int = 7):
    storage = TradingStorage(database_url_from_env())
    return JSONResponse(content=jsonable_encoder(storage.fetch_trades_since(days)))


@app.get("/api/events")
def api_events(days: int = 7):
    storage = TradingStorage(database_url_from_env())
    return JSONResponse(content=jsonable_encoder(storage.fetch_events_since(days)))


@app.get("/api/logs")
def api_logs():
    root = Path("logs")
    return JSONResponse(
        {
            "runtime": _read_tail(root / "runtime.log"),
            "analysis": _read_tail(root / "analysis.log"),
            "orders": _read_tail(root / "orders.log"),
        }
    )


@app.get("/api/runtime-config")
def api_get_runtime_config():
    return JSONResponse(load_runtime_config())


@app.post("/api/runtime-config")
def api_set_runtime_config(req: RuntimeConfigRequest):
    cfg = save_runtime_config(req.model_dump())
    log_json(LOGGERS["runtime"], {"event": "runtime_config_updated", "config": cfg})
    return JSONResponse({"ok": True, "config": cfg})


@app.post("/api/manual-order")
def api_manual_order(req: ManualOrderRequest):
    if req.confirm_live != "YES":
        return JSONResponse({"ok": False, "error": "confirm_live 必须为 YES"}, status_code=400)
    cfg = live_trading_config_from_env()
    if cfg is None:
        return JSONResponse({"ok": False, "error": "缺少 PRIVATE_KEY/FUNDER_ADDRESS"}, status_code=400)
    effective_cfg = LiveTradingConfig(
        private_key=cfg.private_key,
        funder_address=cfg.funder_address,
        signature_type=cfg.signature_type if req.signature_type is None else int(req.signature_type),
    )

    gamma = GammaClient()
    clob = ClobPublicClient()
    try:
        market = gamma.get_market_by_slug(req.slug)
        legs = outcome_legs(market)
        if req.outcome_index < 0 or req.outcome_index >= len(legs):
            return JSONResponse({"ok": False, "error": "outcome_index 越界"}, status_code=400)
        leg = legs[req.outcome_index]
        tob = clob.top_of_book(leg.token_id)
        size = float(req.size)
        if size < float(tob.min_order_size):
            size = float(tob.min_order_size)
        intent = OrderIntent(
            token_id=leg.token_id,
            side=req.side.upper(),  # type: ignore[arg-type]
            price=float(req.price),
            size=size,
            tick_size=tob.tick_size,
            neg_risk=tob.neg_risk,
        )
        client = build_trading_client(effective_cfg)
        resp = submit_limit(client, intent)
        TradingStorage(database_url_from_env()).log_trade(
            {
                "market_slug": str(market.get("slug", "")),
                "token_id": leg.token_id,
                "side": req.side.upper(),
                "price": float(req.price),
                "size": float(size),
                "notional": float(req.price) * float(size),
                "fees": 0.0,
                "impact_cost": 0.0,
                "status": "submitted",
                "order_id": str(resp.get("orderID", "")) if isinstance(resp, dict) else "",
                "metadata": {"response": resp, "source": "dashboard_manual_order"},
            }
        )
        log_json(
            LOGGERS["orders"],
            {
                "event": "dashboard_manual_order",
                "slug": req.slug,
                "outcome_index": req.outcome_index,
                "side": req.side.upper(),
                "price": req.price,
                "size": size,
                "signature_type": effective_cfg.signature_type,
                "response": resp,
            },
        )
        return JSONResponse({"ok": True, "response": resp})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        gamma.close()
        clob.close()


@app.post("/api/close-position")
def api_close_position(req: ClosePositionRequest):
    if req.confirm_live != "YES":
        return JSONResponse({"ok": False, "error": "confirm_live 必须为 YES"}, status_code=400)
    cfg = live_trading_config_from_env()
    if cfg is None:
        return JSONResponse({"ok": False, "error": "缺少 PRIVATE_KEY/FUNDER_ADDRESS"}, status_code=400)

    storage = TradingStorage(database_url_from_env())
    positions = storage.fetch_latest_positions()
    target = next((x for x in positions if str(x.get("token_id", "")) == req.token_id), None)
    if target is None:
        return JSONResponse({"ok": False, "error": "未找到可平仓持仓"}, status_code=404)
    pos = float(target.get("position", 0.0) or 0.0)
    if abs(pos) <= 1e-9:
        return JSONResponse({"ok": False, "error": "当前持仓为 0，无需平仓"}, status_code=400)

    effective_cfg = LiveTradingConfig(
        private_key=cfg.private_key,
        funder_address=cfg.funder_address,
        signature_type=(cfg.signature_type if req.signature_type is None else int(req.signature_type)),
    )
    clob = ClobPublicClient()
    try:
        tob = clob.top_of_book(req.token_id)
        side = "SELL" if pos > 0 else "BUY"
        px = tob.best_bid if side == "SELL" else tob.best_ask
        if px is None:
            return JSONResponse({"ok": False, "error": "盘口为空，无法平仓"}, status_code=400)
        close_size = abs(pos) if req.size is None else min(abs(pos), max(0.0, float(req.size)))
        if close_size <= 0:
            return JSONResponse({"ok": False, "error": "平仓数量必须大于 0"}, status_code=400)
        if close_size < float(tob.min_order_size):
            close_size = float(tob.min_order_size)

        intent = OrderIntent(
            token_id=req.token_id,
            side=side,  # type: ignore[arg-type]
            price=float(px),
            size=float(close_size),
            tick_size=tob.tick_size,
            neg_risk=tob.neg_risk,
        )
        client = build_trading_client(effective_cfg)
        resp = submit_limit(client, intent)

        storage.log_trade(
            {
                "market_slug": req.market_slug,
                "token_id": req.token_id,
                "side": side,
                "price": float(px),
                "size": float(close_size),
                "notional": float(px) * float(close_size),
                "fees": 0.0,
                "impact_cost": 0.0,
                "status": "submitted",
                "order_id": str(resp.get("orderID", "")) if isinstance(resp, dict) else "",
                "metadata": {"response": resp, "source": "dashboard_manual_close"},
            }
        )
        log_json(
            LOGGERS["orders"],
            {
                "event": "dashboard_manual_close",
                "market_slug": req.market_slug,
                "token_id": req.token_id,
                "side": side,
                "price": float(px),
                "size": float(close_size),
                "signature_type": effective_cfg.signature_type,
                "response": resp,
            },
        )
        return JSONResponse({"ok": True, "response": resp, "side": side, "price": float(px), "size": float(close_size)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        clob.close()


@app.get("/", response_class=HTMLResponse)
def index():
    html = """
<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Polymarket Dashboard</title>
  <style>
    body{font-family: ui-sans-serif, system-ui; margin:16px; background:#0b1020; color:#e6edf3}
    h1,h2{margin:8px 0}
    .grid{display:grid; grid-template-columns:1fr 1fr; gap:12px}
    .card{background:#121a2b; border:1px solid #27324a; border-radius:8px; padding:12px}
    pre{white-space:pre-wrap; word-break:break-word; max-height:280px; overflow:auto; background:#0f1524; padding:10px; border-radius:6px}
    table{width:100%; border-collapse:collapse}
    th,td{padding:6px; border-bottom:1px solid #27324a; text-align:left; font-size:13px}
    .muted{color:#8b9bb4; font-size:12px}
    .form-grid{display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:8px}
    .form-grid .full{grid-column:1/-1}
    .input{
      width:100%;
      padding:10px 12px;
      border:1px solid #2c3955;
      border-radius:8px;
      background:#0f1524;
      color:#e6edf3;
      box-sizing:border-box;
      outline:none;
      transition:border-color .15s ease, box-shadow .15s ease, background .15s ease;
    }
    .input::placeholder{color:#71829f}
    .input:focus{
      border-color:#4f8cff;
      box-shadow:0 0 0 3px rgba(79,140,255,.2);
      background:#111a2d;
    }
    .btn{
      appearance:none;
      border:none;
      border-radius:8px;
      padding:10px 14px;
      font-size:13px;
      font-weight:600;
      cursor:pointer;
      transition:transform .05s ease, background .15s ease;
    }
    .btn:active{transform:translateY(1px)}
    .btn-primary{background:#2b6cff;color:#fff}
    .btn-primary:hover{background:#3a78ff}
    .btn-secondary{background:#2a3348;color:#dbe6ff}
    .btn-secondary:hover{background:#32405e}
  </style>
</head>
<body>
  <h1>AI Polymarket Dashboard</h1>
  <div class="muted" id="refresh_status">自动刷新：开启（20 秒）</div>
  <div style="margin:8px 0 12px 0;">
    <button id="toggle_refresh_btn" class="btn btn-secondary" onclick="toggleAutoRefresh()">关闭自动刷新</button>
  </div>
  <div class="grid">
    <div class="card">
      <h2>当前持仓（含手动/自动拆分）</h2>
      <table id="positions"><thead><tr><th>市场</th><th>Token</th><th>总持仓</th><th>手动持仓</th><th>自动持仓</th><th>已实现盈亏</th><th>未实现盈亏</th><th>总盈亏</th><th>操作</th></tr></thead><tbody></tbody></table>
    </div>
    <div class="card">
      <h2>最近交易（7天，全部下单来源）</h2>
      <table id="trades"><thead><tr><th>时间</th><th>市场</th><th>来源</th><th>方向</th><th>价格</th><th>数量</th><th>状态</th></tr></thead><tbody></tbody></table>
    </div>
  </div>
  <div class="grid" style="margin-top:12px;">
    <div class="card">
      <h2>手动下单</h2>
      <div class="muted">需要输入 confirm_live=YES</div>
      <div class="form-grid">
        <input id="ord_slug" class="input full" placeholder="市场 slug" />
        <input id="ord_outcome" class="input" placeholder="outcome_index" value="0" />
        <input id="ord_side" class="input" placeholder="BUY/SELL" value="BUY" />
        <input id="ord_price" class="input" placeholder="price" value="0.45" />
        <input id="ord_size" class="input" placeholder="size" value="5" />
        <input id="ord_sig" class="input" placeholder="signature_type" value="1" />
        <input id="ord_confirm" class="input" placeholder="confirm_live=YES" value="NO" />
        <button class="btn btn-primary full" onclick="manualOrder()">提交订单</button>
      </div>
      <pre id="order_result"></pre>
    </div>
    <div class="card">
      <h2>运行参数面板</h2>
      <div class="muted">保存后自动循环会在下一轮读取</div>
      <div class="form-grid">
        <input id="cfg_live_mode" class="input" placeholder="live_mode true/false" />
        <input id="cfg_sig" class="input" placeholder="signature_type" />
        <input id="cfg_top_markets" class="input" placeholder="top_markets" />
        <input id="cfg_max_orders" class="input" placeholder="max_orders" />
        <input id="cfg_min_confidence" class="input" placeholder="min_confidence" />
        <input id="cfg_default_size" class="input" placeholder="default_size" />
        <input id="cfg_timeout" class="input" placeholder="analysis_timeout_s" />
        <input id="cfg_interval" class="input" placeholder="interval_seconds" />
        <button class="btn btn-secondary full" onclick="saveConfig()">保存配置</button>
      </div>
      <pre id="cfg_result"></pre>
    </div>
  </div>
  <div class="grid" style="margin-top:12px;">
    <div class="card"><h2>运行日志</h2><pre id="runtime"></pre></div>
    <div class="card"><h2>分析日志</h2><pre id="analysis"></pre></div>
  </div>
  <div class="card" style="margin-top:12px;"><h2>下单日志</h2><pre id="orders"></pre></div>
  <script>
    let autoRefreshEnabled = true;
    let refreshTimer = null;
    async function loadConfig(){
      const c=await fetch('/api/runtime-config').then(r=>r.json());
      document.getElementById('cfg_live_mode').value=String(c.live_mode);
      document.getElementById('cfg_top_markets').value=String(c.top_markets);
      document.getElementById('cfg_max_orders').value=String(c.max_orders);
      document.getElementById('cfg_min_confidence').value=String(c.min_confidence);
      document.getElementById('cfg_default_size').value=String(c.default_size);
      document.getElementById('cfg_timeout').value=String(c.analysis_timeout_s);
      document.getElementById('cfg_interval').value=String(c.interval_seconds);
      document.getElementById('cfg_sig').value=String(c.signature_type);
    }
    async function saveConfig(){
      const payload={
        live_mode: String(document.getElementById('cfg_live_mode').value).toLowerCase()==='true',
        top_markets: Number(document.getElementById('cfg_top_markets').value),
        max_orders: Number(document.getElementById('cfg_max_orders').value),
        min_confidence: Number(document.getElementById('cfg_min_confidence').value),
        default_size: Number(document.getElementById('cfg_default_size').value),
        analysis_timeout_s: Number(document.getElementById('cfg_timeout').value),
        interval_seconds: Number(document.getElementById('cfg_interval').value),
        signature_type: Number(document.getElementById('cfg_sig').value),
      };
      const res=await fetch('/api/runtime-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());
      document.getElementById('cfg_result').textContent=JSON.stringify(res,null,2);
    }
    async function manualOrder(){
      const payload={
        slug: document.getElementById('ord_slug').value,
        outcome_index: Number(document.getElementById('ord_outcome').value),
        side: document.getElementById('ord_side').value,
        price: Number(document.getElementById('ord_price').value),
        size: Number(document.getElementById('ord_size').value),
        signature_type: Number(document.getElementById('ord_sig').value),
        confirm_live: document.getElementById('ord_confirm').value
      };
      const res=await fetch('/api/manual-order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());
      document.getElementById('order_result').textContent=JSON.stringify(res,null,2);
      await refresh();
    }
    function maskToken(token){
      const s = String(token || '');
      if(s.length <= 10) return s;
      return `${s.slice(0, 6)}***${s.slice(-4)}`;
    }
    async function closePosition(tokenId, marketSlug, position){
      const confirmLive = prompt(`确认平仓 ${marketSlug}\nToken=${tokenId}\n当前持仓=${position}\n请输入 YES 确认：`, 'NO');
      if(confirmLive !== 'YES') return;
      const payload = { token_id: tokenId, market_slug: marketSlug, confirm_live: 'YES' };
      const res = await fetch('/api/close-position',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
      }).then(r=>r.json()).catch(()=>({ok:false,error:'request failed'}));
      document.getElementById('order_result').textContent=JSON.stringify(res,null,2);
      await refresh();
    }
    async function _safeJson(url, fallback){
      try{
        const resp = await fetch(url);
        if(!resp.ok) return fallback;
        return await resp.json();
      }catch(_e){
        return fallback;
      }
    }
    function nowBeijingText(){
      return new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
    }
    function withBeijingPrefix(text){
      const raw = String(text || '').trimEnd();
      if(!raw) return '';
      const bj = nowBeijingText();
      return raw
        .split('\\n')
        .map(line => `[北京时间 ${bj}] ${line}`)
        .join('\\n');
    }
    async function refresh(){
      const [p,t,l]=await Promise.all([
        _safeJson('/api/positions', []),
        _safeJson('/api/trades?days=7', []),
        _safeJson('/api/logs', {runtime:'',analysis:'',orders:''}),
      ]);
      const pb=document.querySelector('#positions tbody'); pb.innerHTML='';
      (p||[]).forEach(x=>{
        const tr=document.createElement('tr');
        const tokenId = x.token_id || '';
        const marketSlug = x.market_slug || '';
        const pos = Number(x.position ?? 0);
        tr.innerHTML=`<td>${marketSlug}</td><td>${maskToken(tokenId)}</td><td>${(x.position??0).toFixed?.(4) ?? x.position}</td><td>${(x.manual_position??0).toFixed?.(4) ?? x.manual_position}</td><td>${(x.auto_position??0).toFixed?.(4) ?? x.auto_position}</td><td>${(x.realized_pnl??0).toFixed?.(4) ?? x.realized_pnl}</td><td>${(x.unrealized_pnl??0).toFixed?.(4) ?? x.unrealized_pnl}</td><td>${(x.total_pnl??0).toFixed?.(4) ?? x.total_pnl}</td><td class="act"></td>`;
        const btn = document.createElement('button');
        btn.className = 'btn btn-secondary';
        btn.textContent = '手动关单';
        btn.onclick = () => closePosition(tokenId, marketSlug, pos);
        tr.querySelector('.act')?.appendChild(btn);
        pb.appendChild(tr);
      });
      const tb=document.querySelector('#trades tbody'); tb.innerHTML='';
      (t||[]).slice(-100).reverse().forEach(x=>{
        const tr=document.createElement('tr');
        tr.innerHTML=`<td>${x.ts||''}</td><td>${x.market_slug||''}</td><td>${x.source_type||''}</td><td>${x.side||''}</td><td>${x.price||''}</td><td>${x.size||''}</td><td>${x.status||''}</td>`;
        tb.appendChild(tr);
      });
      const runtimeEl = document.getElementById('runtime');
      const analysisEl = document.getElementById('analysis');
      const ordersEl = document.getElementById('orders');
      runtimeEl.textContent = withBeijingPrefix(l.runtime);
      analysisEl.textContent = withBeijingPrefix(l.analysis);
      ordersEl.textContent = withBeijingPrefix(l.orders);
      // 始终滚动到最底部，展示最新日志
      runtimeEl.scrollTop = runtimeEl.scrollHeight;
      analysisEl.scrollTop = analysisEl.scrollHeight;
      ordersEl.scrollTop = ordersEl.scrollHeight;
    }
    function startAutoRefresh(){
      if(refreshTimer) clearInterval(refreshTimer);
      refreshTimer = setInterval(()=>{ if(autoRefreshEnabled) refresh(); }, 20000);
    }
    function toggleAutoRefresh(){
      autoRefreshEnabled = !autoRefreshEnabled;
      const btn = document.getElementById('toggle_refresh_btn');
      const status = document.getElementById('refresh_status');
      if(autoRefreshEnabled){
        btn.textContent = '关闭自动刷新';
        status.textContent = '自动刷新：开启（20 秒）';
        refresh();
      }else{
        btn.textContent = '开启自动刷新';
        status.textContent = '自动刷新：已关闭';
      }
    }
    loadConfig();
    refresh();
    startAutoRefresh();
  </script>
</body>
</html>
"""
    return HTMLResponse(html)
