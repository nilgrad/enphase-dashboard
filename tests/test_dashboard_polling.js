const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS_PATH = path.resolve(__dirname, '..', 'static', 'js', 'dashboard.js');
const DASHBOARD_SOURCE = fs.readFileSync(DASHBOARD_JS_PATH, 'utf8');

function createHarness({ initialHidden = false, initialHash = '', fetchImpl } = {}) {
    let nowMs = 1_700_000_000_000;
    let nextTimerId = 1;
    const timeouts = new Map();
    const intervals = new Map();
    const docListeners = new Map();
    const winListeners = new Map();
    const watchers = new Map();

    const document = {
        hidden: initialHidden,
        addEventListener(type, fn) {
            if (!docListeners.has(type)) docListeners.set(type, new Set());
            docListeners.get(type).add(fn);
        },
        removeEventListener(type, fn) {
            if (docListeners.has(type)) docListeners.get(type).delete(fn);
        },
        dispatchEvent(type) {
            const listeners = docListeners.get(type) || new Set();
            for (const fn of [...listeners]) fn();
        },
        getElementById() {
            return null;
        },
    };

    const windowObj = {
        location: { hash: initialHash },
        addEventListener(type, fn) {
            if (!winListeners.has(type)) winListeners.set(type, new Set());
            winListeners.get(type).add(fn);
        },
        removeEventListener(type, fn) {
            if (winListeners.has(type)) winListeners.get(type).delete(fn);
        },
        dispatchEvent(type) {
            const listeners = winListeners.get(type) || new Set();
            for (const fn of [...listeners]) fn();
        },
    };

    let registeredFactory = null;
    const Alpine = {
        data(name, factory) {
            if (name === 'dashboard') registeredFactory = factory;
        },
    };

    class FakeDate extends Date {
        constructor(...args) {
            super(...(args.length ? args : [nowMs]));
        }
        static now() {
            return nowMs;
        }
    }

    const context = {
        console: { error() {}, log() {}, warn() {} },
        Date: FakeDate,
        Math,
        JSON,
        Promise,
        Array,
        Object,
        Boolean,
        Number,
        String,
        Error,
        parseInt,
        document,
        window: windowObj,
        Alpine,
        Chart: function FakeChart(ctx, cfg) {
            this.cfg = cfg;
            this.destroy = () => {};
        },
        fetch: fetchImpl,
        setTimeout(fn, delay) {
            const id = nextTimerId++;
            timeouts.set(id, { fn, runAt: nowMs + delay, delay });
            return id;
        },
        clearTimeout(id) {
            timeouts.delete(id);
        },
        setInterval(fn, delay) {
            const id = nextTimerId++;
            intervals.set(id, { fn, delay });
            return id;
        },
        clearInterval(id) {
            intervals.delete(id);
        },
    };

    vm.runInNewContext(DASHBOARD_SOURCE, context, { filename: 'dashboard.js' });
    document.dispatchEvent('alpine:init');
    assert.ok(registeredFactory, 'dashboard Alpine component should register on alpine:init');

    const rawComponent = registeredFactory();
    const component = new Proxy(rawComponent, {
        set(target, prop, value) {
            const prev = target[prop];
            target[prop] = value;
            if (prev !== value && watchers.has(prop)) {
                watchers.get(prop)(value, prev);
            }
            return true;
        },
    });
    component.$watch = (prop, cb) => watchers.set(prop, cb);
    component.$nextTick = (cb) => cb();

    const flushMicrotasks = async () => {
        for (let i = 0; i < 10; i++) {
            await Promise.resolve();
        }
    };

    const advanceTime = async (ms) => {
        const target = nowMs + ms;
        while (true) {
            let nextId = null;
            let nextEntry = null;
            for (const [id, entry] of timeouts.entries()) {
                if (entry.runAt <= target && (!nextEntry || entry.runAt < nextEntry.runAt)) {
                    nextId = id;
                    nextEntry = entry;
                }
            }
            if (!nextEntry) break;
            nowMs = nextEntry.runAt;
            timeouts.delete(nextId);
            nextEntry.fn();
            await flushMicrotasks();
        }
        nowMs = target;
    };

    return {
        context,
        component,
        document,
        window: windowObj,
        timeouts,
        intervals,
        watchers,
        flushMicrotasks,
        advanceTime,
        setHidden(hidden) {
            document.hidden = hidden;
            document.dispatchEvent('visibilitychange');
        },
        navigateHash(hash) {
            windowObj.location.hash = hash;
            windowObj.dispatchEvent('hashchange');
        },
    };
}

function okJson(payload) {
    return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(payload),
    });
}

function errJson(status, message) {
    return Promise.resolve({
        ok: false,
        status,
        json: () => Promise.resolve({ error: message }),
    });
}

const SAMPLE_METERS = [
    { eid: 1, measurementType: 'production', meterState: 'enabled', activePower: 2500, actEnergyDlvd: 100000 },
    { eid: 2, measurementType: 'net-consumption', meterState: 'enabled', activePower: -1000, actEnergyDlvd: 20000, actEnergyRcvd: 40000 },
];

const SAMPLE_TODAY = {
    date: '2026-06-11',
    has_data: true,
    production_kwh: 18.5,
    consumption_kwh: 12.0,
    export_kwh: 9.0,
    import_kwh: 2.5,
    net_grid_kwh: 6.5,
    is_net_export: true,
    solar_coverage_pct: 79.2,
    is_stale: false,
};

test('live polling uses chained setTimeout, never fetches /api/history/daily, and polls /api/history/today every 60s', async () => {
    const calls = [];
    const harness = createHarness({
        fetchImpl(url) {
            calls.push(url);
            if (url === '/api/meters') return okJson(SAMPLE_METERS);
            if (url === '/api/history/today') return okJson(SAMPLE_TODAY);
            return okJson([]);
        },
    });

    const { component, intervals, flushMicrotasks, advanceTime } = harness;
    component.refreshRate = 3000;
    component.init();
    await flushMicrotasks();

    assert.equal(intervals.size, 0, 'Must not use blind setInterval');
    assert.deepEqual(calls, ['/api/meters', '/api/history/today']);

    // Over the next 57 seconds (19 ticks at 3s each), only /api/meters should be polled
    calls.length = 0;
    await advanceTime(57_000);
    assert.equal(calls.filter(u => u === '/api/meters').length, 19);
    assert.equal(calls.filter(u => u === '/api/history/today').length, 0);
    assert.equal(calls.filter(u => u.startsWith('/api/history/daily')).length, 0);

    // At 60 seconds, /api/history/today should be fetched again
    calls.length = 0;
    await advanceTime(3_000);
    assert.deepEqual(calls, ['/api/meters', '/api/history/today']);

    // Manual refresh immediately refreshes both meters and today summary even within 60s window
    calls.length = 0;
    component.refreshLiveMeters();
    await flushMicrotasks();
    assert.deepEqual(calls, ['/api/meters', '/api/history/today']);
});

test('in-flight guard and chained setTimeout prevent overlapping fetchMeters calls at 1000ms refresh rate', async () => {
    let activeMetersFetches = 0;
    let maxConcurrentMetersFetches = 0;
    let pendingResolvers = [];

    const harness = createHarness({
        fetchImpl(url) {
            if (url === '/api/meters') {
                activeMetersFetches += 1;
                maxConcurrentMetersFetches = Math.max(maxConcurrentMetersFetches, activeMetersFetches);
                return new Promise((resolve) => {
                    pendingResolvers.push(() => {
                        activeMetersFetches -= 1;
                        resolve({ ok: true, status: 200, json: () => Promise.resolve(SAMPLE_METERS) });
                    });
                });
            }
            if (url === '/api/history/today') return okJson(SAMPLE_TODAY);
            return okJson([]);
        },
    });

    const { component, timeouts, flushMicrotasks, advanceTime } = harness;
    component.refreshRate = 1000;
    component.init();
    await flushMicrotasks();

    assert.equal(activeMetersFetches, 1);
    assert.equal(timeouts.size, 0, 'Next poll must not be scheduled while first fetch is still in flight');

    // Even after 2500ms pass and an extra manual/programmatic fetchMeters() is triggered, no overlap occurs
    await advanceTime(2500);
    component.fetchMeters();
    await flushMicrotasks();
    assert.equal(activeMetersFetches, 1);
    assert.equal(maxConcurrentMetersFetches, 1);

    // Complete first slow fetch -> next poll should be scheduled 1000ms later
    pendingResolvers.shift()();
    await flushMicrotasks();
    assert.equal(activeMetersFetches, 0);
    assert.equal(timeouts.size, 1);

    await advanceTime(999);
    assert.equal(activeMetersFetches, 0);
    await advanceTime(1);
    assert.equal(activeMetersFetches, 1);
    assert.equal(maxConcurrentMetersFetches, 1);
});

test('visibilitychange pauses live polling when hidden and resumes when visible', async () => {
    const calls = [];
    const harness = createHarness({
        fetchImpl(url) {
            calls.push(url);
            if (url === '/api/meters') return okJson(SAMPLE_METERS);
            if (url === '/api/history/today') return okJson(SAMPLE_TODAY);
            return okJson([]);
        },
    });

    const { component, timeouts, flushMicrotasks, advanceTime, setHidden } = harness;
    component.refreshRate = 1000;
    component.init();
    await flushMicrotasks();
    assert.equal(timeouts.size, 1);

    // Background tab -> timer cancelled, zero polls while hidden
    calls.length = 0;
    setHidden(true);
    assert.equal(timeouts.size, 0);
    await advanceTime(10_000);
    assert.equal(calls.length, 0);

    // Foreground tab -> immediately fetches and resumes polling
    setHidden(false);
    await flushMicrotasks();
    assert.deepEqual(calls, ['/api/meters']);
    assert.equal(timeouts.size, 1);

    // When Auto-Refresh is Off (0), visibility resume should NOT auto-poll
    component.refreshRate = 0;
    component.updateRefreshInterval();
    calls.length = 0;
    setHidden(true);
    setHidden(false);
    await flushMicrotasks();
    assert.equal(calls.length, 0);
    assert.equal(timeouts.size, 0);
});

test('fetchSummary requests only the 14-day window via /api/history/daily?days=14', async () => {
    const calls = [];
    const harness = createHarness({
        fetchImpl(url) {
            calls.push(url);
            if (url === '/api/meters') return okJson(SAMPLE_METERS);
            return okJson([]);
        },
    });

    const { component } = harness;
    await component.fetchSummary();
    assert.ok(calls.includes('/api/history/daily?days=14'), `Expected /api/history/daily?days=14 in ${JSON.stringify(calls)}`);
    assert.ok(calls.includes('/api/history/monthly'));
});

test('API failures populate inline error banner state and clear on recovery', async () => {
    let failMeters = true;
    const harness = createHarness({
        fetchImpl(url) {
            if (url === '/api/meters') {
                return failMeters
                    ? errJson(504, 'Enphase Gateway request timed out.')
                    : okJson(SAMPLE_METERS);
            }
            if (url === '/api/history/today') return okJson(SAMPLE_TODAY);
            if (url === '/api/history/refresh') return errJson(429, 'Refresh rate limited.');
            return okJson([]);
        },
    });

    const { component, flushMicrotasks } = harness;
    component.refreshRate = 0;
    component.init();
    await flushMicrotasks();

    assert.ok(component.errorMessage && component.errorMessage.includes('timed out'));

    // Recovery clears error banner automatically
    failMeters = false;
    await component.fetchMeters();
    assert.equal(component.errorMessage, null);

    // Sync failure surfaces inline error without calling window.alert
    await component.syncDatabase();
    assert.ok(component.errorMessage && component.errorMessage.includes('rate limited'));
    component.clearError();
    assert.equal(component.errorMessage, null);
});

test('hashchange listener syncs activeTab on browser Back/Forward navigation', async () => {
    const calls = [];
    const harness = createHarness({
        fetchImpl(url) {
            calls.push(url);
            if (url === '/api/meters') return okJson(SAMPLE_METERS);
            if (url === '/api/history/today') return okJson(SAMPLE_TODAY);
            return okJson([]);
        },
    });

    const { component, flushMicrotasks, navigateHash } = harness;
    component.refreshRate = 0;
    component.init();
    await flushMicrotasks();
    assert.equal(component.activeTab, 'live-tab');

    // Navigate forward to history-tab via hashchange
    calls.length = 0;
    navigateHash('#history-tab');
    await flushMicrotasks();
    assert.equal(component.activeTab, 'history-tab');
    assert.ok(calls.includes('/api/history/daily'));

    // Navigate back to empty/invalid hash -> falls back to live-tab
    navigateHash('');
    await flushMicrotasks();
    assert.equal(component.activeTab, 'live-tab');
});

test('shared effectiveness chart builder guards Math.max against empty arrays', () => {
    const harness = createHarness({ fetchImpl: () => okJson([]) });
    const { buildEffectivenessChartConfig, formatChartDate, formatChartDateLong } = harness.context;

    const emptyCfg = buildEffectivenessChartConfig([], {
        sliceCount: 14,
        barFillAlpha: 0.15,
        pointRadius: 4,
        pointHoverRadius: 6,
        yAxisTitle: 'Daily Energy (kWh)',
        formatLabel: d => formatChartDate(d.date),
        formatTooltipTitle: d => formatChartDateLong(d.date),
        formatEnergyValue: y => y.toFixed(2) + ' kWh',
    });

    assert.equal(emptyCfg.options.scales.y1.max, 100);
    assert.deepEqual(emptyCfg.data.labels, []);

    const highCfg = buildEffectivenessChartConfig(
        [{ date: '2026-06-10', production_kwh: 35, import_kwh: 2, consumption_kwh: 20 }],
        {
            sliceCount: 14,
            barFillAlpha: 0.15,
            pointRadius: 4,
            pointHoverRadius: 6,
            yAxisTitle: 'Daily Energy (kWh)',
            formatLabel: d => formatChartDate(d.date),
            formatTooltipTitle: d => formatChartDateLong(d.date),
            formatEnergyValue: y => y.toFixed(2) + ' kWh',
        }
    );
    // 35/20 * 100 = 175% -> rounded up to nearest 50 = 200
    assert.equal(highCfg.options.scales.y1.max, 200);
});

test('background polling keeps manualRefreshingMeters false while manual refresh toggles it and computes live flow metrics', async () => {
    let pendingMetersResolve = null;
    const harness = createHarness({
        fetchImpl(url) {
            if (url === '/api/meters') {
                return new Promise((resolve) => {
                    pendingMetersResolve = () => resolve({
                        ok: true,
                        status: 200,
                        json: () => Promise.resolve(SAMPLE_METERS),
                    });
                });
            }
            if (url === '/api/history/today') return okJson(SAMPLE_TODAY);
            return okJson([]);
        },
    });

    const { component, flushMicrotasks } = harness;
    component.refreshRate = 0;
    component.init();
    await flushMicrotasks();

    // Initial background fetch should NOT flicker the manual refresh button state
    assert.equal(component.loadingMeters, true);
    assert.equal(component.manualRefreshingMeters, false);

    pendingMetersResolve();
    await flushMicrotasks();
    assert.equal(component.loadingMeters, false);
    assert.equal(component.manualRefreshingMeters, false);
    assert.ok(component.lastMetersUpdateText.length > 0);

    // SAMPLE_METERS: prod=2500W, grid=-1000W (exporting), cons=1500W
    assert.equal(component.isSolarActive, true);
    assert.equal(component.gridFlowDirection, 'export');
    assert.equal(component.liveSolarSharePct, 100);
    assert.equal(component.liveGridSharePct, 0);
    assert.ok(component.liveSourceSummary.includes('100% Solar Powered'));

    // Mixed state: prod=1000W, grid=+1000W (importing), cons=2000W -> 50% solar / 50% grid
    component.prodPower = 1000;
    component.gridPower = 1000;
    component.consPower = 2000;
    assert.equal(component.gridFlowDirection, 'import');
    assert.equal(component.liveSolarSharePct, 50);
    assert.equal(component.liveGridSharePct, 50);

    // Manual refresh SHOULD toggle manualRefreshingMeters while in flight
    component.refreshLiveMeters();
    await flushMicrotasks();
    assert.equal(component.manualRefreshingMeters, true);

    pendingMetersResolve();
    await flushMicrotasks();
    assert.equal(component.manualRefreshingMeters, false);
});

test('history combo chart slices by selected timeframe, summarizes totals, and builds CSV output', () => {
    const harness = createHarness({ fetchImpl: () => okJson([]) });
    const { buildHistoryChartConfig, buildHistoryCsv } = harness.context;
    const { component } = harness;

    const sampleHistory = Array.from({ length: 20 }, (_, idx) => ({
        date: `2026-06-${String(idx + 1).padStart(2, '0')}`,
        production_kwh: 20 + idx,
        export_kwh: 8,
        import_kwh: 4,
        consumption_kwh: 16 + idx,
        is_interpolated: idx === 19,
    }));

    const cfg7 = buildHistoryChartConfig(sampleHistory, 7);
    assert.equal(cfg7.data.labels.length, 7);
    assert.equal(cfg7.data.datasets[0].type, 'line');
    assert.equal(cfg7.data.datasets[0].label, 'House Consumption (kWh)');
    assert.equal(cfg7.data.datasets[1].type, 'bar');

    const cfg30 = buildHistoryChartConfig(sampleHistory, 30);
    assert.equal(cfg30.data.labels.length, 20);

    component.historyLogs = sampleHistory;
    component.historyChartDays = 7;
    assert.equal(component.historyPeriodSummary.days, 7);
    assert.equal(component.historyPeriodSummary.exported, 56);
    assert.equal(component.historyPeriodSummary.imported, 28);
    assert.equal(component.formatCoveragePct({ production_kwh: 25, consumption_kwh: 20 }), '125.0%');
    assert.equal(component.formatCoveragePct({ production_kwh: 0, consumption_kwh: 0 }), '0.0%');

    const csv = buildHistoryCsv(sampleHistory.slice(18));
    const lines = csv.trim().split('\n');
    assert.equal(lines[0], 'Date,Solar Production (kWh),Grid Export (kWh),Grid Import (kWh),House Consumption (kWh),Solar Coverage (%),Status');
    // Newest first (2026-06-20 is interpolated)
    assert.ok(lines[1].startsWith('2026-06-20,39.00,8.00,4.00,35.00,111.4,Estimated'));
    assert.ok(lines[2].startsWith('2026-06-19,38.00,8.00,4.00,34.00,111.8,Normal'));
});
