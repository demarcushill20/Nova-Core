#!/usr/bin/env node
/**
 * TradingView CLI — JSON-over-stdout bridge for NovaTrade.
 *
 * Wraps @mathieuc/tradingview to provide:
 *   fetch-candles  — historical OHLCV data
 *   fetch-indicator — server-side indicator values
 *   run-backtest   — strategy backtest report (trades, metrics, equity)
 *   search-symbol  — symbol search
 *   list-inputs    — list strategy/indicator input parameters
 *
 * Auth: Set TV_SESSION and TV_SIGNATURE env vars (from TradingView cookies).
 *
 * Usage:
 *   node tv-cli.js fetch-candles --symbol OANDA:EURUSD --timeframe 60 --bars 5000
 *   node tv-cli.js run-backtest --symbol OANDA:EURUSD --timeframe 60 --strategy PUB;abc123
 *   node tv-cli.js fetch-indicator --symbol OANDA:EURUSD --timeframe 60 --indicator STD;EMA --option Length=50
 */

const TradingView = require("@mathieuc/tradingview");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function die(msg, code = 1) {
  console.error(JSON.stringify({ error: msg }));
  process.exit(code);
}

function output(data) {
  console.log(JSON.stringify(data));
  process.exit(0);
}

function parseArgs(argv) {
  const args = {};
  let command = null;
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) {
      if (!command) command = a;
      continue;
    }
    const key = a.replace(/^--/, "");
    const next = argv[i + 1];
    if (next && !next.startsWith("--")) {
      args[key] = next;
      i++;
    } else {
      args[key] = true;
    }
  }
  return { command, args };
}

function createClient() {
  const token = process.env.TV_SESSION || "";
  const signature = process.env.TV_SIGNATURE || "";
  if (!token) {
    die("TV_SESSION env var required (TradingView sessionid cookie)");
  }
  return new TradingView.Client({
    token,
    signature,
  });
}

function withTimeout(promise, ms = 30000) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`Timeout after ${ms}ms`)), ms)
    ),
  ]);
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

async function fetchCandles({ symbol, timeframe, bars, from, to }) {
  const client = createClient();
  try {
    const chart = new client.Session.Chart();
    const opts = {
      timeframe: timeframe || "60",
      range: parseInt(bars || "500", 10),
    };
    if (from) opts.from = parseInt(from, 10);
    if (to) opts.to = parseInt(to, 10);

    chart.setMarket(symbol || "OANDA:EURUSD", opts);

    const candles = await withTimeout(
      new Promise((resolve) => {
        chart.onUpdate(() => {
          const periods = chart.periods.map((p) => ({
            timestamp: p.time,
            open: p.open,
            high: p.max,
            low: p.min,
            close: p.close,
            volume: p.volume || 0,
          }));
          resolve(periods);
        });
      }),
      60000
    );

    client.end();
    output({ ok: true, symbol, timeframe: opts.timeframe, candles });
  } catch (err) {
    client.end();
    die(`fetch-candles failed: ${err.message}`);
  }
}

async function fetchIndicator({ symbol, timeframe, indicator, option, bars }) {
  const client = createClient();
  try {
    const chart = new client.Session.Chart();
    chart.setMarket(symbol || "OANDA:EURUSD", {
      timeframe: timeframe || "60",
      range: parseInt(bars || "500", 10),
    });

    const indic = await withTimeout(
      TradingView.getIndicator(indicator || "STD;EMA"),
      15000
    );

    // Parse --option Key=Value pairs
    if (option) {
      const options = Array.isArray(option) ? option : [option];
      for (const opt of options) {
        const [key, val] = opt.split("=");
        const numVal = Number(val);
        indic.setOption(key, isNaN(numVal) ? val : numVal);
      }
    }

    const study = new chart.Study(indic);

    const values = await withTimeout(
      new Promise((resolve) => {
        study.onUpdate(() => {
          resolve(
            study.periods.map((p) => {
              const row = { timestamp: p.$time };
              for (const [k, v] of Object.entries(p)) {
                if (k !== "$time") row[k] = v;
              }
              return row;
            })
          );
        });
      }),
      60000
    );

    client.end();
    output({
      ok: true,
      symbol,
      indicator,
      inputs: indic.options,
      values,
    });
  } catch (err) {
    client.end();
    die(`fetch-indicator failed: ${err.message}`);
  }
}

async function runBacktest({
  symbol,
  timeframe,
  strategy,
  option,
  bars,
}) {
  if (!strategy) die("--strategy required (e.g., PUB;abc123 or script ID)");

  const client = createClient();
  try {
    const chart = new client.Session.Chart();
    chart.setMarket(symbol || "OANDA:EURUSD", {
      timeframe: timeframe || "60",
      range: parseInt(bars || "5000", 10),
    });

    const indic = await withTimeout(
      TradingView.getIndicator(strategy),
      15000
    );

    // Apply input overrides
    if (option) {
      const options = Array.isArray(option) ? option : [option];
      for (const opt of options) {
        const [key, val] = opt.split("=");
        const numVal = Number(val);
        indic.setOption(key, isNaN(numVal) ? val : numVal);
      }
    }

    const study = new chart.Study(indic);

    const report = await withTimeout(
      new Promise((resolve, reject) => {
        let resolved = false;
        study.onUpdate((changes) => {
          if (resolved) return;
          const sr = study.strategyReport;
          if (sr && sr.trades && sr.trades.length > 0) {
            resolved = true;
            resolve(sr);
          }
        });

        // Also handle case where strategy has 0 trades
        study.onReady(() => {
          setTimeout(() => {
            if (!resolved) {
              resolved = true;
              resolve(study.strategyReport || { trades: [], performance: null });
            }
          }, 5000);
        });
      }),
      120000
    );

    client.end();

    // Flatten the report for clean JSON output
    const result = {
      ok: true,
      symbol,
      timeframe: timeframe || "60",
      strategy,
      inputs: indic.options,
      trade_count: report.trades ? report.trades.length : 0,
      trades: (report.trades || []).map((t) => ({
        entry_name: t.entry?.name,
        entry_type: t.entry?.type,
        entry_price: t.entry?.value,
        entry_time: t.entry?.time,
        exit_name: t.exit?.name,
        exit_price: t.exit?.value,
        exit_time: t.exit?.time,
        quantity: t.quantity,
        profit: t.profit,
        cumulative: t.cumulative,
        runup: t.runup,
        drawdown: t.drawdown,
      })),
      performance: report.performance || null,
      equity_curve: report.history || null,
      settings: report.settings || null,
    };

    output(result);
  } catch (err) {
    client.end();
    die(`run-backtest failed: ${err.message}`);
  }
}

async function searchSymbol({ query, type }) {
  try {
    const results = await withTimeout(
      TradingView.searchMarketV3(query || "EURUSD", type || ""),
      15000
    );
    output({ ok: true, results });
  } catch (err) {
    die(`search-symbol failed: ${err.message}`);
  }
}

async function listInputs({ strategy }) {
  if (!strategy) die("--strategy required");
  try {
    const indic = await withTimeout(
      TradingView.getIndicator(strategy),
      15000
    );
    output({
      ok: true,
      strategy,
      description: indic.description,
      inputs: indic.options,
      script_type: indic.scriptIdPart,
    });
  } catch (err) {
    die(`list-inputs failed: ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const COMMANDS = {
  "fetch-candles": fetchCandles,
  "fetch-indicator": fetchIndicator,
  "run-backtest": runBacktest,
  "search-symbol": searchSymbol,
  "list-inputs": listInputs,
};

async function main() {
  const { command, args } = parseArgs(process.argv);

  if (!command || command === "help" || args.help) {
    console.log(`TradingView CLI — JSON bridge for NovaTrade

Commands:
  fetch-candles     Fetch historical OHLCV data
    --symbol        Symbol (default: OANDA:EURUSD)
    --timeframe     Timeframe in minutes (default: 60)
    --bars          Number of bars (default: 500)

  fetch-indicator   Get server-side indicator values
    --symbol        Symbol (default: OANDA:EURUSD)
    --timeframe     Timeframe in minutes (default: 60)
    --indicator     Indicator ID (e.g., STD;EMA)
    --option        Input override as Key=Value (repeatable)
    --bars          Number of bars (default: 500)

  run-backtest      Run strategy backtest, return trades + metrics
    --symbol        Symbol (default: OANDA:EURUSD)
    --timeframe     Timeframe in minutes (default: 60)
    --strategy      Strategy ID (PUB;xxx or private script ID) [required]
    --option        Input override as Key=Value (repeatable)
    --bars          Number of bars (default: 5000)

  search-symbol     Search for symbols
    --query         Search query (default: EURUSD)
    --type          Filter type (forex, crypto, stock, etc.)

  list-inputs       List strategy/indicator input parameters
    --strategy      Script ID [required]

Environment:
  TV_SESSION        TradingView sessionid cookie [required]
  TV_SIGNATURE      TradingView sessionid_sign cookie [required]
`);
    process.exit(0);
  }

  const handler = COMMANDS[command];
  if (!handler) {
    die(`Unknown command: ${command}. Run with 'help' for usage.`);
  }

  await handler(args);
}

main().catch((err) => die(`Fatal: ${err.message}`));
