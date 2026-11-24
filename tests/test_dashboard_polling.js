const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS_PATH = path.resolve(__dirname, '..', 'static', 'js', 'dashboard.js');
const DASHBOARD_SOURCE = fs.readFileSync(DASHBOARD_JS_PATH, 'utf8');

function createHarness({ initialHidden = false, fetchImpl } = {}) {
    let nowMs = 1_700_000_000_000;
    let nextTimerId = 1;
    const timeouts = new Map();
    const intervals = new Map();
    const docListeners = new Map();
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

    const windowObj = { location: { hash: '' } };

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
        Chart: function FakeChart() {
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

    const component = registeredFactory();
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
        component,
        document,
        timeouts,
        intervals,
        watchers,
        flushMicrotasks,
        advanceTime,
        setHidden(hidden) {
            document.hidden = hidden;
            document.dispatchEvent('visibilitychange');
        },
    };
}

function okJson(payload) {
    return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(payload),
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
                        resolve({ ok: true, json: () => Promise.resolve(SAMPLE_METERS) });
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
