let historyChartInstance = null;
let effectivenessChartInstance = null;
let monthlyChartInstance = null;
let lastHistoryChartData = null;
let lastEffectivenessChartData = null;
let lastMonthlyChartData = null;
const THEME_STORAGE_KEY = 'enphase-theme';
const THEME_CYCLE = ['auto', 'dark', 'light'];
const TODAY_SUMMARY_REFRESH_MS = 60000;

function getChartTheme() {
    const fallback = {
        isDark: false,
        fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        gridColor: 'rgba(15, 23, 42, 0.07)',
        tickColor: '#64748b',
        titleColor: '#334155',
        tooltipBg: 'rgba(15, 23, 42, 0.94)',
        tooltipFg: '#f8fafc',
        tooltipBorder: 'rgba(148, 163, 184, 0.25)',
        solar: '#059669',
        solarRgb: '5, 150, 105',
        exportColor: '#0284c7',
        exportRgb: '2, 132, 199',
        importColor: '#e11d48',
        importRgb: '225, 29, 72',
        consumption: '#475569',
        consumptionRgb: '71, 85, 105',
        offset: '#7c3aed',
        offsetRgb: '124, 58, 237',
    };
    if (typeof document === 'undefined' || !document.documentElement || typeof getComputedStyle !== 'function') {
        return fallback;
    }
    const root = document.documentElement;
    const isDark = root.dataset && root.dataset.theme === 'dark';
    const styles = getComputedStyle(root);
    const readVar = (name, def) => (styles.getPropertyValue(name) || '').trim() || def;
    return {
        isDark,
        fontFamily: readVar('--font-sans', fallback.fontFamily),
        gridColor: readVar('--chart-grid', fallback.gridColor),
        tickColor: readVar('--chart-tick', fallback.tickColor),
        titleColor: readVar('--chart-title', fallback.titleColor),
        tooltipBg: readVar('--chart-tooltip-bg', fallback.tooltipBg),
        tooltipFg: readVar('--chart-tooltip-fg', fallback.tooltipFg),
        tooltipBorder: readVar('--chart-tooltip-border', fallback.tooltipBorder),
        solar: readVar('--solar', isDark ? '#10b981' : fallback.solar),
        solarRgb: isDark ? '16, 185, 129' : fallback.solarRgb,
        exportColor: readVar('--export', isDark ? '#0ea5e9' : fallback.exportColor),
        exportRgb: isDark ? '14, 165, 233' : fallback.exportRgb,
        importColor: readVar('--import', isDark ? '#f43f5e' : fallback.importColor),
        importRgb: isDark ? '244, 63, 94' : fallback.importRgb,
        consumption: readVar('--consumption', isDark ? '#94a3b8' : fallback.consumption),
        consumptionRgb: isDark ? '148, 163, 184' : fallback.consumptionRgb,
        offset: readVar('--offset', isDark ? '#8b5cf6' : fallback.offset),
        offsetRgb: isDark ? '139, 92, 246' : fallback.offsetRgb,
    };
}

function rerenderActiveChartsForTheme() {
    if (historyChartInstance && lastHistoryChartData) {
        renderHistoryChart(lastHistoryChartData);
    }
    if (effectivenessChartInstance && lastEffectivenessChartData) {
        renderEffectivenessChart(lastEffectivenessChartData);
    }
    if (monthlyChartInstance && lastMonthlyChartData) {
        renderMonthlyChart(lastMonthlyChartData);
    }
}
const VALID_TABS = [
    'live-tab',
    'summary-tab',
    'history-tab',
    'phase-tab',
    'health-tab',
    'connectivity-tab',
    'events-tab',
    'specs-tab',
];

const SHORT_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const LONG_MONTHS = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
];

function formatChartDate(dateStr) {
    if (!dateStr) return '';
    const parts = dateStr.split('-');
    if (parts.length !== 3) return dateStr;
    const monthIndex = parseInt(parts[1], 10) - 1;
    const day = parseInt(parts[2], 10);
    if (monthIndex >= 0 && monthIndex < 12) {
        return `${SHORT_MONTHS[monthIndex]} ${day}`;
    }
    return dateStr;
}

function formatChartDateLong(dateStr) {
    if (!dateStr) return '';
    const parts = dateStr.split('-');
    if (parts.length !== 3) return dateStr;
    const year = parts[0];
    const monthIndex = parseInt(parts[1], 10) - 1;
    const day = parseInt(parts[2], 10);
    if (monthIndex >= 0 && monthIndex < 12) {
        return `${LONG_MONTHS[monthIndex]} ${day}, ${year}`;
    }
    return dateStr;
}

function formatMonthLabelShort(monthStr) {
    if (!monthStr) return '';
    const parts = monthStr.split('-');
    if (parts.length === 2) {
        const year = parts[0].substring(2);
        const monthIndex = parseInt(parts[1], 10) - 1;
        if (monthIndex >= 0 && monthIndex < 12) {
            return `${SHORT_MONTHS[monthIndex]} '${year}`;
        }
    }
    return monthStr;
}

function formatMonthLabelLong(monthStr) {
    if (!monthStr) return '';
    const parts = monthStr.split('-');
    if (parts.length === 2) {
        const year = parts[0];
        const monthIndex = parseInt(parts[1], 10) - 1;
        if (monthIndex >= 0 && monthIndex < 12) {
            return `${LONG_MONTHS[monthIndex]} ${year}`;
        }
    }
    return monthStr;
}

function formatPower(watts) {
    const absWatts = Math.abs(watts);
    if (absWatts >= 1000000) return (watts / 1000000).toFixed(2) + ' MW';
    if (absWatts >= 1000) return (watts / 1000).toFixed(2) + ' kW';
    return watts.toFixed(0) + ' W';
}

function findProductionAndGridMeters(data) {
    if (!Array.isArray(data) || data.length < 2) {
        return { prodMeter: null, gridMeter: null };
    }
    const findByType = (type) =>
        data.find(m => m && m.measurementType === type && (m.meterState || 'enabled') === 'enabled') ||
        data.find(m => m && m.measurementType === type);
    const prodMeter = findByType('production') || data[0];
    const gridMeter =
        findByType('net-consumption') ||
        findByType('total-consumption') ||
        (data[1] !== prodMeter ? data[1] : data[0]);
    return { prodMeter, gridMeter };
}

function calculateLinearRegression(x, y) {
    const n = x.length;
    if (n === 0) return [];

    let sumX = 0, sumY = 0, sumXY = 0, sumXX = 0;
    for (let i = 0; i < n; i++) {
        sumX += x[i];
        sumY += y[i];
        sumXY += x[i] * y[i];
        sumXX += x[i] * x[i];
    }

    const denominator = (n * sumXX - sumX * sumX);
    if (denominator === 0) {
        return y;
    }
    const slope = (n * sumXY - sumX * sumY) / denominator;
    const intercept = (sumY - slope * sumX) / n;

    return x.map(val => slope * val + intercept);
}

async function parseJsonResponse(res) {
    let data = null;
    try {
        data = await res.json();
    } catch (_err) {
        data = null;
    }
    if (!res.ok) {
        const message = (data && data.error) || `Request failed (HTTP ${res.status})`;
        throw new Error(message);
    }
    if (data === null) {
        throw new Error('Received empty or invalid JSON response from server.');
    }
    return data;
}

function renderHistoryChart(data) {
    lastHistoryChartData = data;
    const canvas = document.getElementById('historyChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    if (historyChartInstance) {
        historyChartInstance.destroy();
    }

    const theme = getChartTheme();

    // Limit chart data to the last 14 days
    const chartData = (data || []).slice(-14);

    const dates = chartData.map(d => formatChartDate(d.date));
    const productions = chartData.map(d => d.production_kwh);
    const exports = chartData.map(d => d.export_kwh);
    const imports = chartData.map(d => d.import_kwh);
    const consumptions = chartData.map(d => d.consumption_kwh);

    historyChartInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: dates,
            datasets: [
                {
                    label: 'Solar Production (kWh)',
                    data: productions,
                    backgroundColor: `rgba(${theme.solarRgb}, 0.82)`,
                    borderColor: theme.solar,
                    borderWidth: 1,
                    borderRadius: 5,
                    borderSkipped: 'bottom'
                },
                {
                    label: 'Grid Export (kWh)',
                    data: exports,
                    backgroundColor: `rgba(${theme.exportRgb}, 0.78)`,
                    borderColor: theme.exportColor,
                    borderWidth: 1,
                    borderRadius: 5,
                    borderSkipped: 'bottom'
                },
                {
                    label: 'Grid Import (kWh)',
                    data: imports,
                    backgroundColor: `rgba(${theme.importRgb}, 0.78)`,
                    borderColor: theme.importColor,
                    borderWidth: 1,
                    borderRadius: 5,
                    borderSkipped: 'bottom'
                },
                {
                    label: 'House Consumption (kWh)',
                    data: consumptions,
                    backgroundColor: `rgba(${theme.consumptionRgb}, 0.75)`,
                    borderColor: theme.consumption,
                    borderWidth: 1,
                    borderRadius: 5,
                    borderSkipped: 'bottom'
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            scales: {
                y: {
                    beginAtZero: true,
                    ticks: {
                        color: theme.tickColor,
                        font: { family: theme.fontFamily }
                    },
                    grid: {
                        color: theme.gridColor
                    },
                    title: {
                        display: true,
                        text: 'Energy (kWh)',
                        color: theme.titleColor,
                        font: { family: theme.fontFamily, weight: '600' }
                    }
                },
                x: {
                    ticks: {
                        color: theme.tickColor,
                        font: { family: theme.fontFamily }
                    },
                    grid: {
                        display: false
                    }
                }
            },
            plugins: {
                legend: {
                    position: 'top',
                    labels: {
                        boxWidth: 10,
                        boxHeight: 10,
                        usePointStyle: true,
                        pointStyle: 'circle',
                        color: theme.titleColor,
                        padding: 16,
                        font: { family: theme.fontFamily, weight: '600' }
                    }
                },
                tooltip: {
                    backgroundColor: theme.tooltipBg,
                    titleColor: theme.tooltipFg,
                    bodyColor: theme.tooltipFg,
                    borderColor: theme.tooltipBorder,
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 10,
                    callbacks: {
                        title: function(context) {
                            const dataIndex = context[0].dataIndex;
                            if (chartData[dataIndex]) {
                                return formatChartDateLong(chartData[dataIndex].date);
                            }
                            return context[0].label;
                        },
                        afterBody: function(items) {
                            const dataIndex = items[0].dataIndex;
                            if (chartData[dataIndex] && chartData[dataIndex].is_interpolated) {
                                return '\n* Estimated data due to server downtime';
                            }
                            return '';
                        }
                    }
                }
            }
        }
    });
}

function buildEffectivenessChartConfig(data, {
    sliceCount,
    barFillAlpha,
    pointRadius,
    pointHoverRadius,
    yAxisTitle,
    formatLabel,
    formatTooltipTitle,
    formatEnergyValue,
}) {
    const theme = getChartTheme();
    const chartData = Array.isArray(data) ? data.slice(-sliceCount) : [];
    const labels = chartData.map(formatLabel);
    const productions = chartData.map(d => d.production_kwh);
    const imports = chartData.map(d => d.import_kwh);
    const effectiveness = chartData.map(d => {
        const cons = d.consumption_kwh;
        if (cons <= 0) return 0;
        return (d.production_kwh / cons) * 100;
    });

    const indices = effectiveness.map((_, idx) => idx);
    const trendValues = calculateLinearRegression(indices, effectiveness);
    const maxEffectiveness = effectiveness.length > 0 ? Math.max(...effectiveness) : 0;
    const y1Max = Math.max(100, Math.ceil(maxEffectiveness / 50) * 50);

    return {
        type: 'bar',
        data: {
            labels,
            datasets: [
                {
                    label: 'Solar Production (kWh)',
                    type: 'bar',
                    data: productions,
                    backgroundColor: `rgba(${theme.solarRgb}, ${Math.max(barFillAlpha, 0.25)})`,
                    borderColor: theme.solar,
                    borderWidth: 1.5,
                    borderRadius: 5,
                    borderSkipped: 'bottom',
                    yAxisID: 'y'
                },
                {
                    label: 'Grid Import (kWh)',
                    type: 'bar',
                    data: imports,
                    backgroundColor: `rgba(${theme.importRgb}, ${Math.max(barFillAlpha, 0.25)})`,
                    borderColor: theme.importColor,
                    borderWidth: 1.5,
                    borderRadius: 5,
                    borderSkipped: 'bottom',
                    yAxisID: 'y'
                },
                {
                    label: 'Solar Effectiveness (%)',
                    type: 'line',
                    data: effectiveness,
                    borderColor: theme.offset,
                    backgroundColor: `rgba(${theme.offsetRgb}, 0.08)`,
                    borderWidth: 2.75,
                    pointBackgroundColor: theme.offset,
                    pointRadius,
                    pointHoverRadius,
                    tension: 0.3,
                    yAxisID: 'y1'
                },
                {
                    label: 'Effectiveness Trend Line',
                    type: 'line',
                    data: trendValues,
                    borderColor: `rgba(${theme.offsetRgb}, 0.65)`,
                    backgroundColor: 'transparent',
                    borderWidth: 2,
                    borderDash: [6, 6],
                    pointRadius: 0,
                    fill: false,
                    tension: 0,
                    yAxisID: 'y1'
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            scales: {
                y: {
                    type: 'linear',
                    display: true,
                    position: 'left',
                    beginAtZero: true,
                    ticks: {
                        color: theme.tickColor,
                        font: { family: theme.fontFamily }
                    },
                    title: {
                        display: true,
                        text: yAxisTitle,
                        color: theme.titleColor,
                        font: { family: theme.fontFamily, weight: '600' }
                    },
                    grid: {
                        color: theme.gridColor
                    }
                },
                y1: {
                    type: 'linear',
                    display: true,
                    position: 'right',
                    beginAtZero: true,
                    max: y1Max,
                    ticks: {
                        color: theme.tickColor,
                        font: { family: theme.fontFamily }
                    },
                    title: {
                        display: true,
                        text: 'Solar Effectiveness (%)',
                        color: theme.titleColor,
                        font: { family: theme.fontFamily, weight: '600' }
                    },
                    grid: {
                        drawOnChartArea: false
                    }
                },
                x: {
                    ticks: {
                        color: theme.tickColor,
                        font: { family: theme.fontFamily }
                    },
                    grid: {
                        display: false
                    }
                }
            },
            plugins: {
                legend: {
                    position: 'top',
                    labels: {
                        boxWidth: 10,
                        boxHeight: 10,
                        usePointStyle: true,
                        color: theme.titleColor,
                        padding: 16,
                        font: { family: theme.fontFamily, weight: '600' }
                    }
                },
                tooltip: {
                    backgroundColor: theme.tooltipBg,
                    titleColor: theme.tooltipFg,
                    bodyColor: theme.tooltipFg,
                    borderColor: theme.tooltipBorder,
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 10,
                    callbacks: {
                        title: function(context) {
                            const dataIndex = context[0].dataIndex;
                            if (chartData[dataIndex]) {
                                return formatTooltipTitle(chartData[dataIndex]);
                            }
                            return context[0].label;
                        },
                        label: function(context) {
                            let label = context.dataset.label || '';
                            if (label) {
                                label += ': ';
                            }
                            if (context.parsed.y !== null) {
                                if (context.datasetIndex < 2) {
                                    label += formatEnergyValue(context.parsed.y);
                                } else {
                                    label += context.parsed.y.toFixed(1) + '%';
                                }
                            }
                            return label;
                        }
                    }
                }
            }
        }
    };
}

function renderEffectivenessChart(data) {
    lastEffectivenessChartData = data;
    const canvas = document.getElementById('effectivenessChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    if (effectivenessChartInstance) {
        effectivenessChartInstance.destroy();
    }

    const config = buildEffectivenessChartConfig(data, {
        sliceCount: 14,
        barFillAlpha: 0.28,
        pointRadius: 4,
        pointHoverRadius: 6,
        yAxisTitle: 'Daily Energy (kWh)',
        formatLabel: d => formatChartDate(d.date),
        formatTooltipTitle: d => formatChartDateLong(d.date),
        formatEnergyValue: y => y.toFixed(2) + ' kWh',
    });
    effectivenessChartInstance = new Chart(ctx, config);
}

function renderMonthlyChart(data) {
    lastMonthlyChartData = data;
    const canvas = document.getElementById('monthlyChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    if (monthlyChartInstance) {
        monthlyChartInstance.destroy();
    }

    const config = buildEffectivenessChartConfig(data, {
        sliceCount: 6,
        barFillAlpha: 0.35,
        pointRadius: 5,
        pointHoverRadius: 7,
        yAxisTitle: 'Monthly Energy (kWh)',
        formatLabel: d => formatMonthLabelShort(d.month),
        formatTooltipTitle: d => formatMonthLabelLong(d.month),
        formatEnergyValue: y => y.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + ' kWh',
    });
    monthlyChartInstance = new Chart(ctx, config);
}

document.addEventListener('alpine:init', () => {
    Alpine.data('dashboard', () => ({
        activeTab: 'live-tab',
        refreshRate: 3000,
        themePreference: 'auto',

        // Inline API error banner state
        errorMessage: null,
        errorSource: null,

        // Live power state
        meters: [],
        prodPower: 0,
        gridPower: 0,
        consPower: 0,
        loadingMeters: false,
        todayDate: '',
        todayProduction: 0,
        todayImport: 0,
        todayExport: 0,
        todayNetGrid: 0,
        todayIsNetExport: true,
        todayConsumption: 0,
        todayCoverage: 0,
        todayStale: false,
        hasTodayStats: false,

        // Summary state
        lifetimeSolar: '',
        lifetimeImported: '',
        lifetimeExported: '',
        lifetimeConsumed: '',
        netSolarOffset: '0.0%',
        loadingSummary: false,
        showEffectivenessChart: false,
        showMonthlyChart: false,

        // History state
        historyLogs: [],
        loadingHistory: false,
        historyPage: 1,
        historyPageSize: 20,

        // Phases state
        phases: [],
        loadingPhases: false,

        // Health state
        healthSummary: {
            operating: 0,
            producing: 0,
            communicating: 0,
            total: 0
        },
        inverters: [],
        loadingHealth: false,

        // Connectivity state
        connectivity: null,
        loadingConnectivity: false,

        // Events state
        events: [],
        loadingEvents: false,

        // Timer & polling state for auto-refresh
        refreshTimer: null,
        metersInFlight: false,
        todayInFlight: false,
        lastTodayFetchAt: 0,
        pollGeneration: 0,
        _visibilityHandler: null,
        _hashChangeHandler: null,
        _themeMediaQuery: null,
        _themeMediaHandler: null,

        get themeLabel() {
            if (this.themePreference === 'dark') return 'Dark';
            if (this.themePreference === 'light') return 'Light';
            return 'Auto';
        },

        initTheme() {
            let saved = 'auto';
            try {
                if (typeof localStorage !== 'undefined') {
                    const stored = localStorage.getItem(THEME_STORAGE_KEY);
                    if (THEME_CYCLE.includes(stored)) {
                        saved = stored;
                    }
                }
            } catch (_err) {
                saved = 'auto';
            }
            this.themePreference = saved;
            this.applyTheme({ rerenderCharts: false });

            if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
                this._themeMediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
                this._themeMediaHandler = () => {
                    if (this.themePreference === 'auto') {
                        this.applyTheme({ rerenderCharts: true });
                    }
                };
                if (typeof this._themeMediaQuery.addEventListener === 'function') {
                    this._themeMediaQuery.addEventListener('change', this._themeMediaHandler);
                }
            }
        },

        applyTheme({ rerenderCharts = true } = {}) {
            if (typeof document === 'undefined' || !document.documentElement) {
                return;
            }
            const prefersDark =
                typeof window !== 'undefined' &&
                typeof window.matchMedia === 'function' &&
                window.matchMedia('(prefers-color-scheme: dark)').matches;
            const resolved = this.themePreference === 'auto'
                ? (prefersDark ? 'dark' : 'light')
                : this.themePreference;
            document.documentElement.dataset.theme = resolved;
            if (rerenderCharts) {
                rerenderActiveChartsForTheme();
            }
        },

        cycleTheme() {
            const idx = THEME_CYCLE.indexOf(this.themePreference);
            const next = THEME_CYCLE[(idx + 1) % THEME_CYCLE.length];
            this.themePreference = next;
            try {
                if (typeof localStorage !== 'undefined') {
                    localStorage.setItem(THEME_STORAGE_KEY, next);
                }
            } catch (_err) {
                // Ignore storage errors in restricted browser modes
            }
            this.applyTheme({ rerenderCharts: true });
        },

        init() {
            this.initTheme();

            // Pause live polling while tab is backgrounded and resume when visible again
            this._visibilityHandler = () => this.handleVisibilityChange();
            document.addEventListener('visibilitychange', this._visibilityHandler);

            // Sync active tab with browser Back/Forward navigation
            this._hashChangeHandler = () => this.handleHashChange();
            window.addEventListener('hashchange', this._hashChangeHandler);

            // Load initial tab from hash
            const hash = window.location.hash.replace('#', '');
            if (hash && VALID_TABS.includes(hash)) {
                this.activeTab = hash;
            } else {
                this.activeTab = 'live-tab';
            }

            // Watch tab changes to load data and handle browser history
            this.$watch('activeTab', (newVal) => {
                if (window.location.hash.replace('#', '') !== newVal) {
                    window.location.hash = newVal;
                }
                this.clearError();
                this.handleTabSwitch(newVal);
            });

            // Trigger load for the initial active tab
            this.handleTabSwitch(this.activeTab);
        },

        destroy() {
            this.stopRefreshTimer();
            if (this._visibilityHandler) {
                document.removeEventListener('visibilitychange', this._visibilityHandler);
                this._visibilityHandler = null;
            }
            if (this._hashChangeHandler) {
                window.removeEventListener('hashchange', this._hashChangeHandler);
                this._hashChangeHandler = null;
            }
            if (this._themeMediaQuery && this._themeMediaHandler && typeof this._themeMediaQuery.removeEventListener === 'function') {
                this._themeMediaQuery.removeEventListener('change', this._themeMediaHandler);
                this._themeMediaQuery = null;
                this._themeMediaHandler = null;
            }
        },

        setError(message, source = null) {
            this.errorMessage = message || 'An unexpected error occurred.';
            this.errorSource = source;
        },

        clearError(source = null) {
            if (!source || this.errorSource === source) {
                this.errorMessage = null;
                this.errorSource = null;
            }
        },

        handleHashChange() {
            const hash = window.location.hash.replace('#', '');
            const targetTab = VALID_TABS.includes(hash) ? hash : 'live-tab';
            if (this.activeTab !== targetTab) {
                this.activeTab = targetTab;
            }
        },

        handleVisibilityChange() {
            if (document.hidden) {
                this.stopRefreshTimer();
            } else if (this.activeTab === 'live-tab' && this.refreshRate > 0) {
                this.startLivePolling();
            }
        },

        openTab(tabName) {
            this.activeTab = tabName;
        },

        handleTabSwitch(tabName) {
            // Manage refresh timers
            if (tabName === 'live-tab') {
                if (!document.hidden) {
                    this.startLivePolling({ forceToday: true });
                } else {
                    this.stopRefreshTimer();
                }
            } else {
                this.stopRefreshTimer();
            }

            // Trigger lazy loading
            this.triggerTabLoad(tabName);
        },

        triggerTabLoad(tabName) {
            if (tabName === 'summary-tab') this.fetchSummary();
            if (tabName === 'history-tab') this.fetchHistory();
            if (tabName === 'phase-tab') this.fetchPhases();
            if (tabName === 'health-tab') this.fetchHealth();
            if (tabName === 'connectivity-tab') this.fetchConnectivity();
            if (tabName === 'events-tab') this.fetchEvents();
        },

        startLivePolling({ forceToday = false } = {}) {
            this.stopRefreshTimer();
            if (this.activeTab !== 'live-tab' || document.hidden) {
                return;
            }
            const gen = this.pollGeneration;
            this.fetchMeters({ forceToday, pollGeneration: gen });
        },

        scheduleNextPoll(gen) {
            if (
                gen !== this.pollGeneration ||
                this.activeTab !== 'live-tab' ||
                this.refreshRate <= 0 ||
                document.hidden
            ) {
                return;
            }
            if (this.refreshTimer !== null) {
                clearTimeout(this.refreshTimer);
                this.refreshTimer = null;
            }
            this.refreshTimer = setTimeout(() => {
                this.refreshTimer = null;
                this.fetchMeters({ pollGeneration: gen });
            }, this.refreshRate);
        },

        updateRefreshInterval() {
            this.stopRefreshTimer();
            if (this.refreshRate > 0 && this.activeTab === 'live-tab' && !document.hidden) {
                this.scheduleNextPoll(this.pollGeneration);
            }
        },

        stopRefreshTimer() {
            this.pollGeneration += 1;
            if (this.refreshTimer !== null) {
                clearTimeout(this.refreshTimer);
                this.refreshTimer = null;
            }
        },

        refreshLiveMeters() {
            if (this.metersInFlight) {
                return;
            }
            if (this.refreshRate > 0 && this.activeTab === 'live-tab' && !document.hidden) {
                this.startLivePolling({ forceToday: true });
            } else {
                this.fetchMeters({ forceToday: true });
            }
        },

        formatPower(watts) {
            return formatPower(watts);
        },

        formatDate(timestamp) {
            if (!timestamp) return 'Never';
            return new Date(timestamp * 1000).toLocaleString();
        },

        formatDateLong(dateStr) {
            return formatChartDateLong(dateStr);
        },

        formatInverterStatus(statusArray) {
            if (!statusArray || statusArray.length === 0) return 'Unknown';
            return statusArray.map(status => {
                if (status === 'envoy.global.ok') return 'Normal';
                if (status.includes('acvoltageoosp1')) return 'AC Voltage OOS';
                return status.replace('envoy.cond_flags.pcu_chan.', '').replace('envoy.', '');
            }).join(', ');
        },

        isOkState(val) {
            if (!val) return false;
            return ['connected', 'ok', 'configured', 'enabled'].includes(val.toLowerCase());
        },

        formatMeterName(meter, idx) {
            if (meter && meter.measurementType === 'production') return 'Production Meter';
            if (meter && meter.measurementType === 'net-consumption') return 'Grid Meter';
            if (meter && meter.measurementType === 'total-consumption') return 'Consumption Meter';
            return idx === 0 ? 'Production Meter' : 'Grid Meter';
        },

        async fetchTodaySummary(force = false) {
            if (this.todayInFlight) {
                return;
            }
            const now = Date.now();
            if (!force && this.lastTodayFetchAt > 0 && (now - this.lastTodayFetchAt) < TODAY_SUMMARY_REFRESH_MS) {
                return;
            }
            this.todayInFlight = true;
            try {
                const todayRes = await fetch('/api/history/today');
                const todayData = await parseJsonResponse(todayRes);
                this.todayDate = todayData.date || '';
                this.todayProduction = todayData.production_kwh || 0;
                this.todayImport = todayData.import_kwh || 0;
                this.todayExport = todayData.export_kwh || 0;
                this.todayNetGrid = todayData.net_grid_kwh || 0;
                this.todayIsNetExport = todayData.is_net_export ?? (this.todayNetGrid >= 0);
                this.todayConsumption = todayData.consumption_kwh || 0;
                this.todayCoverage = todayData.solar_coverage_pct || 0;
                this.todayStale = Boolean(todayData.is_stale);
                this.hasTodayStats = Boolean(todayData.has_data);
                this.lastTodayFetchAt = Date.now();
                this.clearError('today');
            } catch (err) {
                console.error('Fetch today summary error:', err);
                this.setError(`Unable to load today's solar summary: ${err.message}`, 'today');
            } finally {
                this.todayInFlight = false;
            }
        },

        async fetchMeters({ forceToday = false, pollGeneration = null } = {}) {
            if (this.metersInFlight) {
                if (pollGeneration !== null) {
                    this.scheduleNextPoll(pollGeneration);
                }
                return;
            }
            this.metersInFlight = true;
            this.loadingMeters = true;
            try {
                const shouldFetchToday =
                    forceToday ||
                    this.lastTodayFetchAt === 0 ||
                    (Date.now() - this.lastTodayFetchAt) >= TODAY_SUMMARY_REFRESH_MS;

                const metersPromise = (async () => {
                    const res = await fetch('/api/meters');
                    const data = await parseJsonResponse(res);

                    const { prodMeter, gridMeter } = findProductionAndGridMeters(data);
                    if (prodMeter && gridMeter) {
                        this.meters = data;
                        this.prodPower = prodMeter.activePower || 0;
                        this.gridPower = gridMeter.activePower || 0;
                        this.consPower = this.prodPower + this.gridPower;
                    }
                    this.clearError('meters');
                })();

                if (shouldFetchToday) {
                    await Promise.all([metersPromise, this.fetchTodaySummary(forceToday)]);
                } else {
                    await metersPromise;
                }
            } catch (err) {
                console.error('Fetch meters error:', err);
                this.setError(`Unable to load live meter readings: ${err.message}`, 'meters');
            } finally {
                this.loadingMeters = false;
                this.metersInFlight = false;
                if (pollGeneration !== null) {
                    this.scheduleNextPoll(pollGeneration);
                }
            }
        },

        async fetchSummary() {
            this.loadingSummary = true;
            try {
                const [resMeters, resHistory, resMonthly] = await Promise.all([
                    fetch('/api/meters'),
                    fetch('/api/history/daily?days=14'),
                    fetch('/api/history/monthly')
                ]);

                const data = await parseJsonResponse(resMeters);

                const { prodMeter, gridMeter } = findProductionAndGridMeters(data);
                if (prodMeter && gridMeter) {
                    const lifetimeProduction = prodMeter.actEnergyDlvd || 0;
                    const lifetimeImport = gridMeter.actEnergyDlvd || 0;
                    const lifetimeExport = gridMeter.actEnergyRcvd || 0;
                    const lifetimeConsumption = lifetimeProduction - lifetimeExport + lifetimeImport;

                    const formatEnergy = (wh) => {
                        if (wh >= 1000000) return (wh / 1000000).toFixed(2) + ' MWh';
                        if (wh >= 1000) return (wh / 1000).toFixed(2) + ' kWh';
                        return wh.toFixed(0) + ' Wh';
                    };

                    this.lifetimeSolar = formatEnergy(lifetimeProduction);
                    this.lifetimeImported = formatEnergy(lifetimeImport);
                    this.lifetimeExported = formatEnergy(lifetimeExport);
                    this.lifetimeConsumed = formatEnergy(lifetimeConsumption);
                    this.netSolarOffset = lifetimeConsumption > 0
                        ? ((lifetimeProduction / lifetimeConsumption) * 100).toFixed(1) + '%'
                        : '100.0%';
                }

                if (resHistory.ok) {
                    const historyData = await resHistory.json();
                    if (historyData && historyData.length > 0) {
                        this.showEffectivenessChart = true;
                        this.$nextTick(() => {
                            renderEffectivenessChart(historyData);
                        });
                    }
                }

                if (resMonthly.ok) {
                    const monthlyData = await resMonthly.json();
                    if (monthlyData && monthlyData.length > 0) {
                        this.showMonthlyChart = true;
                        this.$nextTick(() => {
                            renderMonthlyChart(monthlyData);
                        });
                    }
                }
                this.clearError('summary');
            } catch (err) {
                console.error('Fetch summary error:', err);
                this.setError(`Unable to load system summary: ${err.message}`, 'summary');
            } finally {
                this.loadingSummary = false;
            }
        },

        async fetchHistory() {
            this.loadingHistory = true;
            try {
                const res = await fetch('/api/history/daily');
                const data = await parseJsonResponse(res);

                this.historyPage = 1;
                this.historyLogs = data;

                if (data.length > 0) {
                    this.$nextTick(() => {
                        renderHistoryChart(data);
                    });
                }
                this.clearError('history');
            } catch (err) {
                console.error('Fetch history error:', err);
                this.setError(`Unable to load energy history: ${err.message}`, 'history');
            } finally {
                this.loadingHistory = false;
            }
        },

        async syncDatabase() {
            this.loadingHistory = true;
            try {
                const res = await fetch('/api/history/refresh', { method: 'POST' });
                await parseJsonResponse(res);
                this.clearError('sync');
                await this.fetchHistory();
            } catch (err) {
                console.error('Sync error:', err);
                this.setError(`Failed to sync database: ${err.message}`, 'sync');
                this.loadingHistory = false;
            }
        },

        async fetchPhases() {
            this.loadingPhases = true;
            try {
                const res = await fetch('/api/meters');
                const data = await parseJsonResponse(res);

                const activePhaseMeters = data.filter(m => {
                    if (!Array.isArray(m.channels) || m.channels.length === 0) return false;
                    if (m.meterState && m.meterState !== 'enabled') return false;
                    if (m.measurementType) {
                        return ['production', 'net-consumption'].includes(m.measurementType);
                    }
                    return true;
                });
                this.phases = activePhaseMeters.length > 0
                    ? activePhaseMeters
                    : data.filter(m => Array.isArray(m.channels) && m.channels.length > 0);
                this.clearError('phases');
            } catch (err) {
                console.error('Fetch phases error:', err);
                this.setError(`Unable to load phase details: ${err.message}`, 'phases');
            } finally {
                this.loadingPhases = false;
            }
        },

        async fetchHealth() {
            this.loadingHealth = true;
            try {
                const res = await fetch('/api/inventory');
                const data = await parseJsonResponse(res);

                const pcuGroup = data.find(g => g.type === 'PCU');
                if (!pcuGroup) throw new Error('No inverters found in inventory');

                this.inverters = pcuGroup.devices || [];

                // Fix the Envoy firmware bug where operating is false even when inverters are normal
                this.inverters.forEach(inv => {
                    inv.operating = inv.producing || (inv.device_status && inv.device_status.includes('envoy.global.ok'));
                });

                this.healthSummary = {
                    total: this.inverters.length,
                    operating: this.inverters.filter(i => i.operating).length,
                    producing: this.inverters.filter(i => i.producing).length,
                    communicating: this.inverters.filter(i => i.communicating).length
                };
                this.clearError('health');
            } catch (err) {
                console.error('Fetch health error:', err);
                this.setError(`Unable to load inverter health: ${err.message}`, 'health');
            } finally {
                this.loadingHealth = false;
            }
        },

        get paginatedHistoryLogs() {
            const reversed = [...this.historyLogs].reverse();
            const start = (this.historyPage - 1) * this.historyPageSize;
            const end = start + this.historyPageSize;
            return reversed.slice(start, end);
        },

        get totalHistoryPages() {
            return Math.ceil(this.historyLogs.length / this.historyPageSize) || 1;
        },

        get visibleHistoryPages() {
            const pages = [];
            const total = this.totalHistoryPages;
            const current = this.historyPage;
            const range = 2;
            for (let i = 1; i <= total; i++) {
                if (i === 1 || i === total || (i >= current - range && i <= current + range)) {
                    pages.push(i);
                } else if (pages[pages.length - 1] !== '...') {
                    pages.push('...');
                }
            }
            return pages;
        },

        prevHistoryPage() {
            if (this.historyPage > 1) {
                this.historyPage--;
            }
        },

        nextHistoryPage() {
            if (this.historyPage < this.totalHistoryPages) {
                this.historyPage++;
            }
        },

        setHistoryPage(page) {
            if (page >= 1 && page <= this.totalHistoryPages) {
                this.historyPage = page;
            }
        },

        async fetchConnectivity() {
            this.loadingConnectivity = true;
            try {
                const res = await fetch('/api/connectivity');
                const data = await parseJsonResponse(res);

                const conn = data.connection;
                this.connectivity = {
                    mqtt_state: conn.mqtt_state || 'unknown',
                    prov_state: conn.prov_state || 'unknown',
                    auth_state: conn.auth_state || 'unknown',
                    sc_stream: conn.sc_stream || 'unknown',
                    sc_debug: conn.sc_debug || 'unknown',
                    meters_last_update: data.meters?.last_update || null
                };
                this.clearError('connectivity');
            } catch (err) {
                console.error('Fetch connectivity error:', err);
                this.setError(`Unable to load gateway connectivity: ${err.message}`, 'connectivity');
            } finally {
                this.loadingConnectivity = false;
            }
        },

        async fetchEvents() {
            this.loadingEvents = true;
            try {
                const res = await fetch('/api/events');
                const data = await parseJsonResponse(res);

                this.events = data.alerts || [];
                this.clearError('events');
            } catch (err) {
                console.error('Fetch events error:', err);
                this.setError(`Unable to load system events: ${err.message}`, 'events');
            } finally {
                this.loadingEvents = false;
            }
        }
    }));
});
