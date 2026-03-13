import { useState, useEffect, useRef, useCallback } from "react";

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// NEXUS TRADER — Secure Dashboard
// Auth + Portfolio + Bot Control + Deposits/Withdrawals
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

// ── Simulated Auth & Data Layer ──────────────────────────────────────
// En production, remplace par des appels à ton backend FastAPI + JWT

const AUTH_CREDENTIALS = { pin: "1234" }; // En prod: bcrypt + JWT + 2FA

const generateMockData = () => {
  const now = Date.now();
  const trades = [];
  const equity = [];
  let balance = 10000;
  let btcHeld = 0;
  let price = 64000 + Math.random() * 3000;

  for (let i = 0; i < 90; i++) {
    const change = (Math.random() - 0.48) * 0.025;
    price *= (1 + change);
    const action = Math.random();
    
    if (action > 0.85 && balance > 500) {
      const amount = (balance * 0.1) / price;
      balance -= amount * price;
      btcHeld += amount;
      trades.push({
        id: now - (90 - i) * 3600000,
        type: "BUY",
        pair: ["BTC/USDT", "ETH/USDT", "SOL/USDT"][Math.floor(Math.random() * 3)],
        price: +price.toFixed(2),
        amount: +amount.toFixed(6),
        total: +(amount * price).toFixed(2),
        time: new Date(now - (90 - i) * 3600000).toISOString(),
        strategy: ["combined", "rsi_reversal", "ma_crossover"][Math.floor(Math.random() * 3)],
        pnl: null,
      });
    } else if (action < 0.12 && btcHeld > 0.0001) {
      const pnl = (Math.random() - 0.4) * 200;
      balance += btcHeld * price;
      trades.push({
        id: now - (90 - i) * 3600000,
        type: "SELL",
        pair: "BTC/USDT",
        price: +price.toFixed(2),
        amount: +btcHeld.toFixed(6),
        total: +(btcHeld * price).toFixed(2),
        time: new Date(now - (90 - i) * 3600000).toISOString(),
        strategy: "combined",
        pnl: +pnl.toFixed(2),
      });
      btcHeld = 0;
    }

    equity.push({
      time: new Date(now - (90 - i) * 3600000).toLocaleDateString("fr-FR", { day: "2-digit", month: "short" }),
      value: +(balance + btcHeld * price).toFixed(2),
    });
  }

  return {
    balance: { USDT: +balance.toFixed(2), BTC: +btcHeld.toFixed(6) },
    portfolioValue: +(balance + btcHeld * price).toFixed(2),
    price: +price.toFixed(2),
    trades: trades.reverse(),
    equity,
    deposits: [
      { id: 1, type: "deposit", amount: 10000, currency: "USDT", time: "2025-01-15", status: "completed", tx: "0x8a3f...e91b" },
      { id: 2, type: "deposit", amount: 5000, currency: "USDT", time: "2025-02-20", status: "completed", tx: "0x2b7c...d42a" },
    ],
    withdrawals: [
      { id: 3, type: "withdrawal", amount: 2000, currency: "USDT", time: "2025-03-01", status: "completed", tx: "0xf1d2...8c3e" },
    ],
    botStatus: {
      isRunning: true,
      mode: "paper",
      strategy: "combined",
      uptime: "4j 12h 37m",
      lastTrade: "il y a 23 min",
      totalPnl: 847.32,
      winRate: 61.2,
      totalTrades: 156,
      shieldLevel: "NORMAL",
      regime: "mild_uptrend",
      sentiment: "BULLISH",
      sentimentScore: 0.42,
      fearGreed: 65,
      activePositions: 3,
      maxPositions: 5,
    },
    alerts: [
      { id: 1, type: "info", msg: "Bot redémarré après mise à jour", time: "il y a 2h" },
      { id: 2, type: "trade", msg: "ACHAT 0.015 BTC @ $65,420", time: "il y a 23m" },
      { id: 3, type: "warning", msg: "Volatilité élevée détectée (P87)", time: "il y a 1h" },
    ],
  };
};

// ── Micro Sparkline Chart ────────────────────────────────────────────
const Sparkline = ({ data, width = 200, height = 50, color = "#00ff88" }) => {
  if (!data || data.length < 2) return null;
  const values = data.map(d => d.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  
  const points = values.map((v, i) => {
    const x = (i / (values.length - 1)) * width;
    const y = height - ((v - min) / range) * (height - 8) - 4;
    return `${x},${y}`;
  }).join(" ");

  const areaPoints = points + ` ${width},${height} 0,${height}`;
  const isUp = values[values.length - 1] >= values[0];
  const c = isUp ? color : "#ff3366";

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <defs>
        <linearGradient id={`grad-${color}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={c} stopOpacity="0.2" />
          <stop offset="100%" stopColor={c} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={areaPoints} fill={`url(#grad-${color})`} />
      <polyline points={points} fill="none" stroke={c} strokeWidth="2" strokeLinejoin="round" />
    </svg>
  );
};

// ── Pin Input Component ──────────────────────────────────────────────
const PinInput = ({ onSubmit, error }) => {
  const [pin, setPin] = useState(["", "", "", ""]);
  const refs = [useRef(), useRef(), useRef(), useRef()];

  const handleChange = (index, value) => {
    if (!/^\d?$/.test(value)) return;
    const newPin = [...pin];
    newPin[index] = value;
    setPin(newPin);
    if (value && index < 3) refs[index + 1].current?.focus();
    if (newPin.every(d => d !== "")) onSubmit(newPin.join(""));
  };

  const handleKeyDown = (index, e) => {
    if (e.key === "Backspace" && !pin[index] && index > 0) {
      refs[index - 1].current?.focus();
    }
  };

  return (
    <div style={{ display: "flex", gap: 12, justifyContent: "center" }}>
      {pin.map((digit, i) => (
        <input
          key={i}
          ref={refs[i]}
          type="password"
          maxLength={1}
          value={digit}
          onChange={e => handleChange(i, e.target.value)}
          onKeyDown={e => handleKeyDown(i, e)}
          style={{
            width: 56, height: 64, textAlign: "center",
            fontSize: 24, fontFamily: "'Fira Code', monospace",
            background: digit ? "rgba(0,255,136,0.08)" : "rgba(255,255,255,0.03)",
            border: `2px solid ${error ? "#ff3366" : digit ? "rgba(0,255,136,0.3)" : "rgba(255,255,255,0.08)"}`,
            borderRadius: 12, color: "#e0f0ff", outline: "none",
            transition: "all 0.2s",
          }}
          onFocus={e => e.target.style.borderColor = "#00ff88"}
          onBlur={e => e.target.style.borderColor = digit ? "rgba(0,255,136,0.3)" : "rgba(255,255,255,0.08)"}
        />
      ))}
    </div>
  );
};

// ── Login Screen ─────────────────────────────────────────────────────
const LoginScreen = ({ onLogin }) => {
  const [error, setError] = useState(false);
  const [attempts, setAttempts] = useState(0);
  const [locked, setLocked] = useState(false);

  const handleSubmit = (pin) => {
    if (locked) return;
    
    if (pin === AUTH_CREDENTIALS.pin) {
      onLogin();
    } else {
      setError(true);
      setAttempts(a => a + 1);
      setTimeout(() => setError(false), 1500);
      
      if (attempts >= 4) {
        setLocked(true);
        setTimeout(() => { setLocked(false); setAttempts(0); }, 30000);
      }
    }
  };

  return (
    <div style={{
      minHeight: "100vh",
      background: "radial-gradient(ellipse at 30% 20%, #0a1a2e 0%, #060d15 50%, #030608 100%)",
      display: "flex", alignItems: "center", justifyContent: "center",
      fontFamily: "'Outfit', system-ui, sans-serif",
    }}>
      <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;600&family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet" />
      
      <div style={{
        width: 380, padding: "48px 40px",
        background: "linear-gradient(160deg, rgba(15,25,40,0.95), rgba(8,14,20,0.98))",
        border: "1px solid rgba(255,255,255,0.06)",
        borderRadius: 24,
        boxShadow: "0 30px 80px rgba(0,0,0,0.6), 0 0 1px rgba(255,255,255,0.1)",
        backdropFilter: "blur(20px)",
        textAlign: "center",
      }}>
        {/* Logo */}
        <div style={{
          width: 64, height: 64, margin: "0 auto 24px",
          background: "linear-gradient(135deg, #00ff88, #00bbff)",
          borderRadius: 18, display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 28, boxShadow: "0 8px 30px rgba(0,255,136,0.25)",
        }}>⚡</div>
        
        <h1 style={{
          fontSize: 22, fontWeight: 700, color: "#e0f0ff",
          fontFamily: "'Fira Code', monospace", letterSpacing: -0.5, margin: "0 0 6px",
        }}>NEXUS TRADER</h1>
        
        <p style={{ fontSize: 13, color: "#4a7090", margin: "0 0 36px", fontWeight: 300 }}>
          Entre ton code PIN pour accéder au dashboard
        </p>

        {locked ? (
          <div style={{
            padding: "16px 20px", borderRadius: 12,
            background: "rgba(255,51,102,0.08)", border: "1px solid rgba(255,51,102,0.2)",
            color: "#ff6688", fontSize: 13,
          }}>
            🔒 Trop de tentatives. Réessaie dans 30 secondes.
          </div>
        ) : (
          <>
            <PinInput onSubmit={handleSubmit} error={error} />
            {error && (
              <p style={{ color: "#ff3366", fontSize: 12, marginTop: 16, fontFamily: "monospace" }}>
                Code incorrect ({5 - attempts} tentatives restantes)
              </p>
            )}
          </>
        )}

        <p style={{ fontSize: 11, color: "#2a4060", marginTop: 32 }}>
          PIN par défaut: 1234 (à changer en production)
        </p>
      </div>
    </div>
  );
};

// ── Stat Badge ───────────────────────────────────────────────────────
const Badge = ({ label, value, sub, color = "#e0f0ff", icon }) => (
  <div style={{
    background: "linear-gradient(145deg, rgba(15,25,40,0.8), rgba(8,14,20,0.9))",
    border: "1px solid rgba(255,255,255,0.05)",
    borderRadius: 16, padding: "18px 22px",
    flex: 1, minWidth: 160,
    backdropFilter: "blur(10px)",
  }}>
    <div style={{ fontSize: 11, color: "#4a7090", fontFamily: "'Fira Code', monospace", letterSpacing: 1.2, marginBottom: 8 }}>
      {icon} {label}
    </div>
    <div style={{ fontSize: 24, fontWeight: 700, color, fontFamily: "'Fira Code', monospace", lineHeight: 1.2 }}>{value}</div>
    {sub && <div style={{ fontSize: 11, color: "#3a6080", marginTop: 6 }}>{sub}</div>}
  </div>
);

// ── Main Dashboard ───────────────────────────────────────────────────
const Dashboard = ({ data, onAction }) => {
  const [tab, setTab] = useState("overview");
  const bot = data.botStatus;

  const navItems = [
    { id: "overview", label: "Vue d'ensemble", icon: "📊" },
    { id: "portfolio", label: "Portfolio", icon: "💰" },
    { id: "trades", label: "Historique", icon: "📋" },
    { id: "bot", label: "Contrôle Bot", icon: "🤖" },
    { id: "funds", label: "Fonds", icon: "🏦" },
  ];

  return (
    <div style={{
      minHeight: "100vh",
      background: "radial-gradient(ellipse at 20% 10%, #0a1a2e 0%, #060d15 40%, #030608 100%)",
      fontFamily: "'Outfit', system-ui, sans-serif",
      color: "#b0c8e0",
    }}>
      <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
      
      <style>{`
        @keyframes fadeIn { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
        @keyframes pulse2 { 0%,100%{opacity:1} 50%{opacity:0.4} }
        @keyframes shimmer { 0%{background-position:-200%} 100%{background-position:200%} }
        .nav-btn { transition: all 0.2s; }
        .nav-btn:hover { background: rgba(255,255,255,0.04) !important; }
        .card { animation: fadeIn 0.4s ease-out both; }
        .row-hover:hover { background: rgba(255,255,255,0.02) !important; }
        ::-webkit-scrollbar { width:5px; } ::-webkit-scrollbar-track { background:transparent; } ::-webkit-scrollbar-thumb { background:#1a2a3a; border-radius:3px; }
      `}</style>

      {/* ── Header ── */}
      <header style={{
        padding: "14px 28px",
        borderBottom: "1px solid rgba(255,255,255,0.04)",
        display: "flex", justifyContent: "space-between", alignItems: "center",
        background: "rgba(6,13,21,0.8)", backdropFilter: "blur(20px)",
        position: "sticky", top: 0, zIndex: 100,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{
            width: 34, height: 34, borderRadius: 10,
            background: "linear-gradient(135deg, #00ff88, #00bbff)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 16, boxShadow: "0 4px 15px rgba(0,255,136,0.2)",
          }}>⚡</div>
          <div>
            <span style={{ fontFamily: "'Fira Code', monospace", fontSize: 15, fontWeight: 700, color: "#e0f0ff" }}>
              NEXUS
            </span>
            <span style={{ fontSize: 10, color: "#3a6080", marginLeft: 8, fontFamily: "monospace", letterSpacing: 2 }}>
              {bot.mode.toUpperCase()} MODE
            </span>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          {/* Status pill */}
          <div style={{
            display: "flex", alignItems: "center", gap: 8,
            padding: "6px 14px", borderRadius: 20,
            background: bot.isRunning ? "rgba(0,255,136,0.06)" : "rgba(255,60,90,0.06)",
            border: `1px solid ${bot.isRunning ? "rgba(0,255,136,0.15)" : "rgba(255,60,90,0.15)"}`,
          }}>
            <div style={{
              width: 7, height: 7, borderRadius: "50%",
              background: bot.isRunning ? "#00ff88" : "#ff3c5a",
              animation: bot.isRunning ? "pulse2 2s infinite" : "none",
              boxShadow: bot.isRunning ? "0 0 8px rgba(0,255,136,0.5)" : "none",
            }} />
            <span style={{ fontSize: 11, fontFamily: "monospace", color: bot.isRunning ? "#00ff88" : "#ff3c5a" }}>
              {bot.isRunning ? "LIVE" : "OFF"}
            </span>
          </div>
          
          {/* Shield */}
          <div style={{
            padding: "5px 12px", borderRadius: 8, fontSize: 10,
            fontFamily: "monospace", letterSpacing: 0.5,
            background: bot.shieldLevel === "NORMAL" ? "rgba(0,255,136,0.06)" : "rgba(255,200,0,0.08)",
            color: bot.shieldLevel === "NORMAL" ? "#00cc66" : "#ffaa00",
            border: `1px solid ${bot.shieldLevel === "NORMAL" ? "rgba(0,255,136,0.1)" : "rgba(255,200,0,0.15)"}`,
          }}>
            🛡️ {bot.shieldLevel}
          </div>

          {/* Logout */}
          <button onClick={() => onAction("logout")} style={{
            background: "none", border: "1px solid rgba(255,255,255,0.06)",
            borderRadius: 8, padding: "6px 14px", cursor: "pointer",
            color: "#4a7090", fontSize: 11, fontFamily: "monospace",
          }}>
            ← Déconnexion
          </button>
        </div>
      </header>

      <div style={{ display: "flex", minHeight: "calc(100vh - 55px)" }}>
        {/* ── Sidebar Nav ── */}
        <nav style={{
          width: 220, padding: "24px 12px",
          borderRight: "1px solid rgba(255,255,255,0.03)",
          background: "rgba(6,10,18,0.5)",
        }}>
          {navItems.map(item => (
            <button
              key={item.id}
              className="nav-btn"
              onClick={() => setTab(item.id)}
              style={{
                display: "flex", alignItems: "center", gap: 10,
                width: "100%", padding: "11px 14px", marginBottom: 4,
                borderRadius: 10, border: "none", cursor: "pointer",
                fontSize: 13, fontWeight: tab === item.id ? 600 : 400,
                fontFamily: "'Outfit', sans-serif",
                background: tab === item.id ? "rgba(0,255,136,0.06)" : "transparent",
                color: tab === item.id ? "#00ff88" : "#5a8aaa",
                textAlign: "left",
              }}
            >
              <span style={{ fontSize: 16 }}>{item.icon}</span>
              {item.label}
            </button>
          ))}

          {/* Quick stats sidebar */}
          <div style={{
            marginTop: 32, padding: "16px 14px",
            background: "rgba(255,255,255,0.02)", borderRadius: 12,
            border: "1px solid rgba(255,255,255,0.03)",
          }}>
            <div style={{ fontSize: 10, color: "#3a5a7a", fontFamily: "monospace", letterSpacing: 1, marginBottom: 12 }}>
              RÉSUMÉ RAPIDE
            </div>
            {[
              { l: "Sentiment", v: bot.sentiment, c: bot.sentiment === "BULLISH" ? "#00ff88" : "#ff3366" },
              { l: "Fear & Greed", v: `${bot.fearGreed}/100`, c: bot.fearGreed > 60 ? "#00ff88" : "#ffa500" },
              { l: "Régime", v: bot.regime.replace("_", " "), c: "#00bbff" },
              { l: "Positions", v: `${bot.activePositions}/${bot.maxPositions}`, c: "#e0f0ff" },
            ].map(({ l, v, c }) => (
              <div key={l} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid rgba(255,255,255,0.02)" }}>
                <span style={{ fontSize: 11, color: "#4a6a8a" }}>{l}</span>
                <span style={{ fontSize: 11, fontFamily: "monospace", fontWeight: 600, color: c }}>{v}</span>
              </div>
            ))}
          </div>
        </nav>

        {/* ── Main Content ── */}
        <main style={{ flex: 1, padding: 28, overflow: "auto" }}>
          
          {/* ══ OVERVIEW TAB ══ */}
          {tab === "overview" && (
            <div className="card">
              <h2 style={{ fontSize: 18, fontWeight: 600, color: "#e0f0ff", margin: "0 0 24px", fontFamily: "'Fira Code', monospace" }}>
                Vue d'ensemble
              </h2>

              {/* Top stats */}
              <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 28 }}>
                <Badge icon="💎" label="PORTFOLIO" value={`$${data.portfolioValue.toLocaleString()}`} color="#e0f0ff" sub={`${data.balance.BTC} BTC + $${data.balance.USDT.toLocaleString()} USDT`} />
                <Badge icon="📈" label="P&L TOTAL" value={`+$${bot.totalPnl.toFixed(2)}`} color="#00ff88" sub={`${bot.totalTrades} trades | ${bot.winRate}% win rate`} />
                <Badge icon="⚡" label="STRATÉGIE" value={bot.strategy.toUpperCase()} color="#00bbff" sub={`Uptime: ${bot.uptime}`} />
                <Badge icon="🕐" label="DERNIER TRADE" value={bot.lastTrade} color="#ffd700" sub="Voir l'historique →" />
              </div>

              {/* Equity curve */}
              <div style={{
                background: "rgba(10,18,30,0.6)", borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.04)", padding: "20px 24px", marginBottom: 24,
              }}>
                <div style={{ fontSize: 11, color: "#4a7090", fontFamily: "monospace", letterSpacing: 1, marginBottom: 14 }}>
                  COURBE D'ÉQUITÉ — 90 DERNIERS JOURS
                </div>
                <Sparkline data={data.equity} width={680} height={140} color="#00ff88" />
              </div>

              {/* Recent alerts */}
              <div style={{
                background: "rgba(10,18,30,0.6)", borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.04)", padding: "20px 24px",
              }}>
                <div style={{ fontSize: 11, color: "#4a7090", fontFamily: "monospace", letterSpacing: 1, marginBottom: 14 }}>
                  ALERTES RÉCENTES
                </div>
                {data.alerts.map(a => (
                  <div key={a.id} style={{
                    display: "flex", alignItems: "center", gap: 12, padding: "10px 0",
                    borderBottom: "1px solid rgba(255,255,255,0.02)", fontSize: 13,
                  }}>
                    <span style={{
                      width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                      background: a.type === "trade" ? "#00ff88" : a.type === "warning" ? "#ffa500" : "#00bbff",
                    }} />
                    <span style={{ flex: 1, color: "#b0c8e0" }}>{a.msg}</span>
                    <span style={{ fontSize: 11, color: "#3a5a7a", fontFamily: "monospace" }}>{a.time}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ══ PORTFOLIO TAB ══ */}
          {tab === "portfolio" && (
            <div className="card">
              <h2 style={{ fontSize: 18, fontWeight: 600, color: "#e0f0ff", margin: "0 0 24px", fontFamily: "'Fira Code', monospace" }}>
                Portfolio
              </h2>

              {/* Total value */}
              <div style={{
                background: "linear-gradient(135deg, rgba(0,255,136,0.04), rgba(0,187,255,0.03))",
                borderRadius: 20, padding: "32px 36px", marginBottom: 28,
                border: "1px solid rgba(0,255,136,0.08)",
              }}>
                <div style={{ fontSize: 12, color: "#4a7090", fontFamily: "monospace", letterSpacing: 1 }}>VALEUR TOTALE</div>
                <div style={{ fontSize: 42, fontWeight: 700, color: "#e0f0ff", fontFamily: "'Fira Code', monospace", margin: "8px 0" }}>
                  ${data.portfolioValue.toLocaleString()}
                </div>
                <div style={{ fontSize: 14, color: "#00ff88" }}>
                  +${bot.totalPnl.toFixed(2)} ({((bot.totalPnl / 10000) * 100).toFixed(2)}%) depuis le début
                </div>
              </div>

              {/* Balances */}
              <div style={{ fontSize: 11, color: "#4a7090", fontFamily: "monospace", letterSpacing: 1, marginBottom: 14 }}>
                RÉPARTITION DES ACTIFS
              </div>
              {[
                { asset: "USDT", amount: data.balance.USDT, value: data.balance.USDT, pct: (data.balance.USDT / data.portfolioValue * 100) },
                { asset: "BTC", amount: data.balance.BTC, value: data.balance.BTC * data.price, pct: (data.balance.BTC * data.price / data.portfolioValue * 100) },
              ].map(b => (
                <div key={b.asset} className="row-hover" style={{
                  display: "flex", alignItems: "center", gap: 16, padding: "16px 20px",
                  background: "rgba(10,18,30,0.6)", borderRadius: 12, marginBottom: 8,
                  border: "1px solid rgba(255,255,255,0.03)",
                }}>
                  <div style={{
                    width: 40, height: 40, borderRadius: 12,
                    background: b.asset === "USDT" ? "rgba(0,255,136,0.1)" : "rgba(255,170,0,0.1)",
                    display: "flex", alignItems: "center", justifyContent: "center",
                    fontSize: 18,
                  }}>{b.asset === "USDT" ? "💵" : "₿"}</div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, color: "#e0f0ff" }}>{b.asset}</div>
                    <div style={{ fontSize: 12, color: "#4a7090", fontFamily: "monospace" }}>{b.amount.toLocaleString()}</div>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <div style={{ fontSize: 14, fontWeight: 600, color: "#e0f0ff", fontFamily: "monospace" }}>${b.value.toLocaleString()}</div>
                    <div style={{ fontSize: 12, color: "#4a7090" }}>{b.pct.toFixed(1)}%</div>
                  </div>
                  {/* Allocation bar */}
                  <div style={{ width: 80, height: 6, background: "rgba(255,255,255,0.04)", borderRadius: 3 }}>
                    <div style={{
                      width: `${b.pct}%`, height: "100%", borderRadius: 3,
                      background: b.asset === "USDT" ? "#00ff88" : "#ffa500",
                    }} />
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* ══ TRADES TAB ══ */}
          {tab === "trades" && (
            <div className="card">
              <h2 style={{ fontSize: 18, fontWeight: 600, color: "#e0f0ff", margin: "0 0 24px", fontFamily: "'Fira Code', monospace" }}>
                Historique des trades
              </h2>

              <div style={{
                background: "rgba(10,18,30,0.6)", borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.04)", overflow: "hidden",
              }}>
                {/* Header */}
                <div style={{
                  display: "grid", gridTemplateColumns: "70px 100px 100px 100px 90px 80px 1fr",
                  gap: 8, padding: "12px 20px",
                  background: "rgba(255,255,255,0.02)",
                  fontSize: 10, color: "#3a5a7a", fontFamily: "monospace", letterSpacing: 1,
                }}>
                  <span>TYPE</span><span>PAIRE</span><span>PRIX</span><span>MONTANT</span><span>TOTAL</span><span>P&L</span><span>DATE</span>
                </div>

                {/* Rows */}
                <div style={{ maxHeight: 500, overflow: "auto" }}>
                  {data.trades.slice(0, 30).map(t => (
                    <div key={t.id} className="row-hover" style={{
                      display: "grid", gridTemplateColumns: "70px 100px 100px 100px 90px 80px 1fr",
                      gap: 8, padding: "12px 20px",
                      borderBottom: "1px solid rgba(255,255,255,0.02)",
                      fontSize: 12, fontFamily: "monospace", alignItems: "center",
                    }}>
                      <span style={{
                        display: "inline-block", padding: "3px 8px", borderRadius: 6, fontSize: 10, fontWeight: 600, textAlign: "center",
                        background: t.type === "BUY" ? "rgba(0,255,136,0.1)" : "rgba(255,51,102,0.1)",
                        color: t.type === "BUY" ? "#00ff88" : "#ff3366",
                      }}>{t.type}</span>
                      <span style={{ color: "#b0c8e0" }}>{t.pair}</span>
                      <span style={{ color: "#e0f0ff" }}>${t.price.toLocaleString()}</span>
                      <span style={{ color: "#6a9aba" }}>{t.amount}</span>
                      <span style={{ color: "#e0f0ff" }}>${t.total.toLocaleString()}</span>
                      <span style={{ color: t.pnl === null ? "#3a5a7a" : t.pnl >= 0 ? "#00ff88" : "#ff3366" }}>
                        {t.pnl === null ? "—" : `${t.pnl >= 0 ? "+" : ""}$${t.pnl}`}
                      </span>
                      <span style={{ color: "#3a5a7a", fontSize: 11 }}>
                        {new Date(t.time).toLocaleDateString("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* ══ BOT CONTROL TAB ══ */}
          {tab === "bot" && (
            <div className="card">
              <h2 style={{ fontSize: 18, fontWeight: 600, color: "#e0f0ff", margin: "0 0 24px", fontFamily: "'Fira Code', monospace" }}>
                Contrôle du Bot
              </h2>

              {/* Bot status card */}
              <div style={{
                display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 28,
              }}>
                {/* Left: status */}
                <div style={{
                  background: "rgba(10,18,30,0.6)", borderRadius: 16, padding: "24px 28px",
                  border: "1px solid rgba(255,255,255,0.04)",
                }}>
                  <div style={{ fontSize: 11, color: "#4a7090", fontFamily: "monospace", letterSpacing: 1, marginBottom: 16 }}>
                    ÉTAT DU BOT
                  </div>
                  {[
                    { l: "Statut", v: bot.isRunning ? "En marche" : "Arrêté", c: bot.isRunning ? "#00ff88" : "#ff3c5a" },
                    { l: "Mode", v: bot.mode.toUpperCase(), c: bot.mode === "paper" ? "#ffa500" : "#ff3366" },
                    { l: "Stratégie", v: bot.strategy, c: "#00bbff" },
                    { l: "Uptime", v: bot.uptime, c: "#e0f0ff" },
                    { l: "Shield", v: bot.shieldLevel, c: bot.shieldLevel === "NORMAL" ? "#00ff88" : "#ffa500" },
                    { l: "Régime marché", v: bot.regime.replace("_", " "), c: "#b0c8e0" },
                  ].map(({ l, v, c }) => (
                    <div key={l} style={{ display: "flex", justifyContent: "space-between", padding: "8px 0", borderBottom: "1px solid rgba(255,255,255,0.02)" }}>
                      <span style={{ fontSize: 13, color: "#6a8aaa" }}>{l}</span>
                      <span style={{ fontSize: 13, fontFamily: "monospace", fontWeight: 600, color: c }}>{v}</span>
                    </div>
                  ))}
                </div>

                {/* Right: actions */}
                <div style={{
                  background: "rgba(10,18,30,0.6)", borderRadius: 16, padding: "24px 28px",
                  border: "1px solid rgba(255,255,255,0.04)",
                }}>
                  <div style={{ fontSize: 11, color: "#4a7090", fontFamily: "monospace", letterSpacing: 1, marginBottom: 16 }}>
                    ACTIONS
                  </div>

                  {[
                    { label: bot.isRunning ? "⏹ Arrêter le bot" : "▶ Démarrer le bot", color: bot.isRunning ? "#ff3c5a" : "#00ff88", action: "toggle_bot" },
                    { label: "🔄 Changer de stratégie", color: "#00bbff", action: "change_strategy" },
                    { label: "🔬 Lancer un backtest", color: "#ffd700", action: "run_backtest" },
                    { label: "🔍 Scanner le marché", color: "#aa88ff", action: "scan_market" },
                    { label: "🚨 Arrêt d'urgence", color: "#ff3366", action: "emergency_stop" },
                  ].map(btn => (
                    <button key={btn.action} onClick={() => onAction(btn.action)} style={{
                      width: "100%", padding: "12px 18px", marginBottom: 8,
                      borderRadius: 10, border: `1px solid ${btn.color}30`,
                      background: `${btn.color}08`, color: btn.color,
                      fontSize: 13, fontFamily: "'Outfit', sans-serif", fontWeight: 500,
                      cursor: "pointer", textAlign: "left",
                      transition: "all 0.2s",
                    }}
                    onMouseEnter={e => e.target.style.background = `${btn.color}15`}
                    onMouseLeave={e => e.target.style.background = `${btn.color}08`}
                    >
                      {btn.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Performance metrics */}
              <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
                <Badge icon="🏆" label="WIN RATE" value={`${bot.winRate}%`} color="#00ff88" sub={`Sur ${bot.totalTrades} trades`} />
                <Badge icon="💰" label="P&L TOTAL" value={`+$${bot.totalPnl.toFixed(2)}`} color="#00ff88" />
                <Badge icon="🧠" label="SENTIMENT" value={bot.sentiment} color={bot.sentiment === "BULLISH" ? "#00ff88" : "#ff3366"} sub={`Score: ${bot.sentimentScore.toFixed(2)}`} />
                <Badge icon="😱" label="FEAR & GREED" value={bot.fearGreed.toString()} color={bot.fearGreed > 50 ? "#00ff88" : "#ff3366"} sub={bot.fearGreed > 75 ? "Extreme Greed" : bot.fearGreed > 50 ? "Greed" : bot.fearGreed > 25 ? "Fear" : "Extreme Fear"} />
              </div>
            </div>
          )}

          {/* ══ FUNDS TAB ══ */}
          {tab === "funds" && (
            <div className="card">
              <h2 style={{ fontSize: 18, fontWeight: 600, color: "#e0f0ff", margin: "0 0 24px", fontFamily: "'Fira Code', monospace" }}>
                Gestion des fonds
              </h2>

              {/* Action buttons */}
              <div style={{ display: "flex", gap: 14, marginBottom: 28 }}>
                <button onClick={() => onAction("deposit")} style={{
                  flex: 1, padding: "18px 24px", borderRadius: 14,
                  background: "linear-gradient(135deg, rgba(0,255,136,0.08), rgba(0,187,255,0.05))",
                  border: "1px solid rgba(0,255,136,0.15)", cursor: "pointer",
                  color: "#00ff88", fontSize: 15, fontWeight: 600,
                  fontFamily: "'Outfit', sans-serif",
                  transition: "all 0.2s",
                }}>
                  ↓ Déposer des fonds
                </button>
                <button onClick={() => onAction("withdraw")} style={{
                  flex: 1, padding: "18px 24px", borderRadius: 14,
                  background: "rgba(255,255,255,0.02)",
                  border: "1px solid rgba(255,255,255,0.06)", cursor: "pointer",
                  color: "#b0c8e0", fontSize: 15, fontWeight: 600,
                  fontFamily: "'Outfit', sans-serif",
                  transition: "all 0.2s",
                }}>
                  ↑ Retirer des fonds
                </button>
              </div>

              {/* Info box */}
              <div style={{
                padding: "16px 20px", borderRadius: 12, marginBottom: 24,
                background: "rgba(0,187,255,0.04)", border: "1px solid rgba(0,187,255,0.1)",
                fontSize: 13, color: "#6aaacc", lineHeight: 1.6,
              }}>
                ℹ️ Les dépôts et retraits se font directement via Binance. Le bot trade sur ton compte Binance — 
                il n'a accès qu'au trading, jamais aux retraits (si tu as bien configuré les permissions API).
                Pour déposer : envoie des USDT sur ton wallet Binance. Le bot les utilisera automatiquement.
              </div>

              {/* Transaction history */}
              <div style={{ fontSize: 11, color: "#4a7090", fontFamily: "monospace", letterSpacing: 1, marginBottom: 14 }}>
                HISTORIQUE DES MOUVEMENTS
              </div>

              <div style={{
                background: "rgba(10,18,30,0.6)", borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.04)", overflow: "hidden",
              }}>
                {[...data.deposits, ...data.withdrawals]
                  .sort((a, b) => new Date(b.time) - new Date(a.time))
                  .map(tx => (
                    <div key={tx.id} className="row-hover" style={{
                      display: "flex", alignItems: "center", gap: 16, padding: "16px 20px",
                      borderBottom: "1px solid rgba(255,255,255,0.02)",
                    }}>
                      <div style={{
                        width: 40, height: 40, borderRadius: 12,
                        background: tx.type === "deposit" ? "rgba(0,255,136,0.08)" : "rgba(255,100,100,0.08)",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: 18,
                      }}>
                        {tx.type === "deposit" ? "↓" : "↑"}
                      </div>
                      <div style={{ flex: 1 }}>
                        <div style={{ fontSize: 14, fontWeight: 600, color: "#e0f0ff" }}>
                          {tx.type === "deposit" ? "Dépôt" : "Retrait"}
                        </div>
                        <div style={{ fontSize: 11, color: "#3a5a7a", fontFamily: "monospace" }}>
                          {tx.tx}
                        </div>
                      </div>
                      <div style={{ textAlign: "right" }}>
                        <div style={{
                          fontSize: 15, fontWeight: 600, fontFamily: "monospace",
                          color: tx.type === "deposit" ? "#00ff88" : "#ff6688",
                        }}>
                          {tx.type === "deposit" ? "+" : "-"}${tx.amount.toLocaleString()}
                        </div>
                        <div style={{ fontSize: 11, color: "#3a5a7a" }}>{tx.time}</div>
                      </div>
                      <span style={{
                        padding: "4px 10px", borderRadius: 20, fontSize: 10, fontFamily: "monospace",
                        background: "rgba(0,255,136,0.06)", color: "#00cc66",
                      }}>
                        {tx.status}
                      </span>
                    </div>
                  ))}
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
};

// ── Root App ─────────────────────────────────────────────────────────
export default function NexusDashboard() {
  const [authenticated, setAuthenticated] = useState(false);
  const [data, setData] = useState(null);
  const [actionModal, setActionModal] = useState(null);

  useEffect(() => {
    if (authenticated && !data) {
      setData(generateMockData());
    }
  }, [authenticated]);

  // Refresh data periodically
  useEffect(() => {
    if (!authenticated) return;
    const interval = setInterval(() => {
      setData(prev => {
        if (!prev) return generateMockData();
        // Simulate small changes
        const newPrice = prev.price * (1 + (Math.random() - 0.48) * 0.005);
        return {
          ...prev,
          price: +newPrice.toFixed(2),
          portfolioValue: +(prev.balance.USDT + prev.balance.BTC * newPrice).toFixed(2),
        };
      });
    }, 5000);
    return () => clearInterval(interval);
  }, [authenticated]);

  const handleAction = (action) => {
    if (action === "logout") {
      setAuthenticated(false);
      setData(null);
    } else if (action === "emergency_stop") {
      if (confirm("⚠️ ARRÊT D'URGENCE\n\nCeci va fermer TOUTES les positions et arrêter le bot.\nEs-tu sûr ?")) {
        setData(prev => ({
          ...prev,
          botStatus: { ...prev.botStatus, isRunning: false, shieldLevel: "BLACK" },
        }));
      }
    } else if (action === "toggle_bot") {
      setData(prev => ({
        ...prev,
        botStatus: { ...prev.botStatus, isRunning: !prev.botStatus.isRunning },
      }));
    } else {
      setActionModal(action);
      setTimeout(() => setActionModal(null), 2000);
    }
  };

  if (!authenticated) {
    return <LoginScreen onLogin={() => setAuthenticated(true)} />;
  }

  if (!data) return null;

  return (
    <>
      <Dashboard data={data} onAction={handleAction} />
      {actionModal && (
        <div style={{
          position: "fixed", bottom: 24, right: 24,
          padding: "14px 24px", borderRadius: 12,
          background: "rgba(0,255,136,0.1)", border: "1px solid rgba(0,255,136,0.2)",
          color: "#00ff88", fontSize: 13, fontFamily: "monospace",
          animation: "fadeIn 0.3s ease-out",
          zIndex: 1000,
        }}>
          ✅ Action "{actionModal}" envoyée au bot
        </div>
      )}
    </>
  );
}
