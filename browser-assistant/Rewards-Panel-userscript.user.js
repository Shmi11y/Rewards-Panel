// ==UserScript==
// @name         Microsoft Rewards 每日任务助手
// @namespace    brh.daily.tasks
// @version      1.1.0
// @description  自动完成 Bing Rewards 每日搜索与每日活动：热搜与联想词双词源、高斯随机间隔、分组休息、拟人浏览、积分监控、风控自动暂停、跨天重置、多标签页锁、可视化控制台。
// @match        *://www.bing.com/*
// @match        *://cn.bing.com/*
// @match        *://bing.com/*
// @match        *://rewards.bing.com/*
// @icon         https://www.bing.com/favicon.ico
// @run-at       document-idle
// @noframes
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @grant        GM_addStyle
// @grant        GM_openInTab
// @grant        GM_notification
// @grant        GM_xmlhttpRequest
// @connect      bing.com
// @connect      127.0.0.1
// @connect      rewards.bing.com
// @connect      api.bing.com
// @connect      gmya.net
// @connect      api.gmya.net
// @connect      vvhan.com
// @connect      *
// @license      GNU GPLv3
// ==/UserScript==

(function () {
    'use strict';
    const PANEL_BRIDGE = null;
    const SCRIPT_VERSION = '1.1.0';

    /* ============================================================
     * 0. 基础工具
     * ============================================================ */
    const $ = (s, r = document) => r.querySelector(s);
    const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const rand = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
    const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
    const uniq = (arr) => [...new Set(arr)];
    const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

    /** Box-Muller 高斯随机：比均匀随机更像真人节奏 */
    function gauss(mean, sd) {
        let u = 0, v = 0;
        while (!u) u = Math.random();
        while (!v) v = Math.random();
        const z = Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
        return clamp(mean + z * sd, mean - 2.5 * sd, mean + 2.5 * sd);
    }

    function rndStr(len, chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') {
        let s = '';
        for (let i = 0; i < len; i++) s += chars.charAt(Math.floor(Math.random() * chars.length));
        return s;
    }
    const hex32 = () => Array.from({ length: 32 }, () => '0123456789abcdef'[rand(0, 15)]).join('');

    function todayStr() {
        const d = new Date();
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    }

    function fmtDur(ms) {
        if (!ms || ms <= 0) return '--';
        const s = Math.ceil(ms / 1000);
        if (s < 60) return s + ' 秒';
        const m = Math.floor(s / 60), r = s % 60;
        return m + ' 分' + (r ? ' ' + r + ' 秒' : '');
    }

    /* ============================================================
     * 1. 存储（GM 优先，退回 localStorage）
     * ============================================================ */
    const canGM = typeof GM_getValue === 'function' && typeof GM_setValue === 'function';
    const store = {
        get(k, d) {
            try {
                if (canGM) {
                    const v = GM_getValue(k);
                    return (v === undefined || v === null) ? d : v;
                }
                const raw = localStorage.getItem('brh:' + k);
                return raw == null ? d : JSON.parse(raw);
            } catch (e) { return d; }
        },
        set(k, v) {
            try {
                if (canGM) GM_setValue(k, v);
                else localStorage.setItem('brh:' + k, JSON.stringify(v));
            } catch (e) { /* ignore */ }
        },
        del(k) {
            try {
                if (canGM && typeof GM_deleteValue === 'function') GM_deleteValue(k);
                else localStorage.removeItem('brh:' + k);
            } catch (e) { /* ignore */ }
        }
    };

    /* ============================================================
     * 2. 配置
     * ============================================================ */
    const DEFAULT_CFG = {
        target: 30,          // 每日搜索次数
        gapMin: 15,          // 组内间隔下限（秒）
        gapMax: 30,          // 组内间隔上限（秒）
        groupSize: 4,        // 每 N 次进入一次长休息
        restMin: 6,          // 长休息下限（分钟）
        restMax: 10,         // 长休息上限（分钟）
        dwellMin: 8,         // 结果页停留下限（秒）
        dwellMax: 15,        // 结果页停留上限（秒）
        warmup: 3,           // 热身：前 N 次节奏放慢 60%
        fatigue: true,       // 疲劳模拟：越往后越慢
        nightSlow: true,     // 深夜（23:00-07:00）整体放慢
        useHot: true,        // 使用热搜 + Bing 联想词
        apiKey: '',          // gmya.net 可选 API Key
        autoActivity: true,  // 搜索完成后自动打开每日活动
        maxActivities: 8,    // 单次最多打开活动数
        notify: true,        // 桌面通知
        showPanel: true,     // 显示控制台
        tolerance: 5         // 连续 N 次未涨积分 → 判定受限并暂停
    };

    let cfg = Object.assign({}, DEFAULT_CFG, (() => {
        const s = store.get('cfg', {});
        return (s && typeof s === 'object') ? s : {};
    })());
    const saveCfg = () => store.set('cfg', cfg);

    const REWARDS_URL = 'https://rewards.bing.com/dashboard';
    const HOT_API = 'https://api.gmya.net/Api/';
    const HOT_SOURCES = ['BaiduHot', 'ZhiHuHot', 'WeiBoHot', 'TouTiaoHot', 'DouYinHot'];

    /* ============================================================
     * 3. 本地语料（网络词源全挂时的兜底，保证永远有词可用）
     * ============================================================ */
    const LOCAL_BASE = [
        '盛年不重来 一日难再晨', '千里之行 始于足下', '少年易学老难成', '敏而好学 不耻下问',
        '海内存知己 天涯若比邻', '三人行必有我师', '莫愁前路无知己', '天生我材必有用',
        '海纳百川 有容乃大', '穷则独善其身 达则兼济天下', '读书破万卷 下笔如有神',
        '学而不思则罔', '一年之计在于春', '莫等闲 白了少年头', '少壮不努力 老大徒伤悲',
        '一寸光阴一寸金', '近朱者赤 近墨者黑', '吾生也有涯 而知也无涯',
        '纸上得来终觉浅', '己所不欲 勿施于人', '鞠躬尽瘁 死而后已', '书到用时方恨少',
        '天下兴亡 匹夫有责', '人无远虑 必有近忧', '生于忧患 死于安乐',
        '新能源汽车 固态电池 进展', 'iPhone 电池续航 优化', 'Python 多线程 与 协程 区别',
        'Stable Diffusion 提示词 写法', '房贷利率 转换 怎么算', '考研 调剂 流程 时间节点',
        '显示器和显卡 刷新率 匹配', '路由器 5G 和 2.4G 区别', '固态硬盘 掉速 原因',
        '胃食管反流 饮食 注意', '跑步 膝盖 保护 方法', '咖啡 一天 喝多少 合适',
        'Excel 数据透视表 教程', 'Word 目录 自动生成', 'PDF 转 Word 不丢格式',
        '儿童 近视 防控 有效方法', '空气炸锅 和 烤箱 区别', '扫地机器人 值得买吗',
        '深度学习 模型 剪枝', 'rust 所有权 通俗解释', 'git rebase 和 merge 区别',
        'Docker 镜像 体积 优化', '家里 wifi 信号 差 怎么解决', '冬季 空调 除霜 原理',
        '如何 挑选 人体工学椅', '降噪耳机 原理', '电动牙刷 声波 和 旋转 区别',
        '个人所得税 专项附加扣除 标准', '公积金 提取 条件', '社保 断缴 影响',
        '高血压 日常 监测 方法', '失眠 认知行为 疗法', '维生素 D 缺乏 症状',
        '兵马俑 出土 时间', '敦煌 莫高窟 开放 洞窟', '长江 三峡 蓄水 水位',
        '二十四节气 顺序', '京剧 四大行当', '唐诗 三百首 推荐 版本',
        'c 语言 指针 数组 区别', '浏览器 缓存 机制', 'http3 和 http2 区别',
        '正则表达式 常用 写法', 'mysql 索引 失效 场景', 'redis 持久化 方式',
        '云计算 iaas paas saas 区别', '大模型 微调 和 提示词 区别', '向量数据库 用途'
    ];
    const TAILS = ['', '', '', ' 最新消息', ' 是什么原理', ' 怎么解决', ' 2026 更新',
        ' 对比评测', ' 官方文档', ' 常见问题', ' 值得买吗', ' 注意事项', ' 入门教程'];
    const HEADS = ['', '', '', '', '如何', '为什么', '怎么', '2026'];

    function genLocalWords(n, used) {
        const out = [];
        let guard = 0;
        while (out.length < n && guard++ < n * 12) {
            let w = pick(HEADS) + pick(LOCAL_BASE) + pick(TAILS);
            w = w.replace(/\s+/g, ' ').trim();
            if (used.includes(w) || out.includes(w)) continue;
            out.push(w);
        }
        return out;
    }

    /* ============================================================
     * 4. 网络请求（GM_xmlhttpRequest 优先，绕过 CORS）
     * ============================================================ */
    function httpGet(url, type = 'json', timeout = 8000) {
        return new Promise((resolve, reject) => {
            const done = (txt) => {
                try { resolve(type === 'json' ? JSON.parse(txt) : txt); }
                catch (e) { reject(e); }
            };
            if (typeof GM_xmlhttpRequest === 'function') {
                GM_xmlhttpRequest({
                    method: 'GET', url, timeout,
                    headers: { 'Accept': 'application/json, text/plain, */*' },
                    onload: (r) => (r.status >= 200 && r.status < 400) ? done(r.responseText) : reject(new Error('HTTP ' + r.status)),
                    onerror: () => reject(new Error('network')),
                    ontimeout: () => reject(new Error('timeout'))
                });
            } else {
                const ctl = new AbortController();
                const t = setTimeout(() => ctl.abort(), timeout);
                fetch(url, { signal: ctl.signal })
                    .then((r) => r.text())
                    .then((txt) => { clearTimeout(t); done(txt); })
                    .catch((e) => { clearTimeout(t); reject(e); });
            }
        });
    }

    async function panelRequest(action, details = {}) {
        if (!PANEL_BRIDGE || typeof GM_xmlhttpRequest !== 'function') return null;
        try {
            return await new Promise(resolve => {
                const timer = setTimeout(() => resolve(null), 3500);
                const done = result => { clearTimeout(timer); resolve(result); };
                GM_xmlhttpRequest({
                    method: 'POST', url: PANEL_BRIDGE.url, anonymous: true,
                    headers: { 'Content-Type': 'application/json', 'X-Rewards-Script': PANEL_BRIDGE.token },
                    data: JSON.stringify({ action, ...details }),
                    onload: response => {
                        try { done(response.status === 200 ? JSON.parse(response.responseText) : null); }
                        catch { done(null); }
                    },
                    onerror: () => done(null), ontimeout: () => done(null), onabort: () => done(null)
                });
            });
        } catch { return null; }
    }

    /** 热搜榜 */
    async function fetchHotWords() {
        const list = [];
        // gmya 聚合（可选 key）
        try {
            const api = HOT_API + pick(HOT_SOURCES) + '?format=json' + (cfg.apiKey ? '&appkey=' + encodeURIComponent(cfg.apiKey) : '');
            const d = await httpGet(api);
            if (d && Array.isArray(d.data)) {
                d.data.forEach((it) => {
                    const t = it && (it.title || it.name || it.word || it.hot_word);
                    if (t) list.push(String(t));
                });
            }
        } catch (e) { /* 忽略，继续下一个源 */ }
        // 备用源
        try {
            const d = await httpGet('https://api.vvhan.com/api/hotlist/baiduRD?type=json');
            const arr = (d && (d.data || d.data_list)) || [];
            arr.forEach((it) => { if (it && it.title) list.push(String(it.title)); });
        } catch (e) { /* ignore */ }
        return list.filter((w) => w && w.length > 1 && w.length < 40);
    }

    /** Bing 官方联想词：真实用户搜过的词，最自然 */
    async function bingSuggest(seed) {
        try {
            const d = await httpGet('https://api.bing.com/osjson.aspx?query=' + encodeURIComponent(seed) + '&form=QBLH', 'json', 6000);
            if (Array.isArray(d) && Array.isArray(d[1])) return d[1].map(String).filter(Boolean);
        } catch (e) { /* ignore */ }
        return [];
    }

    /* ============================================================
     * 5. 状态（按日期自动重置，跨页面持久化）
     * ============================================================ */
    function blankState(date) {
        return {
            date: date, count: 0, running: false, paused: false, phase: 'idle',
            nextAt: 0, currentWord: '', status: '待机中', pool: [], usedWords: [],
            pointsBase: null, pointsNow: null, noPoint: 0,
            actsDate: '', actsKeys: [], logs: []
        };
    }

    function loadState() {
        const st = store.get('state', null);
        const d = todayStr();
        if (!st || typeof st !== 'object' || st.date !== d) {
            const fresh = blankState(d);
            // 跨天：保留日志尾巴便于排查
            if (st && Array.isArray(st.logs)) fresh.logs = st.logs.slice(-10);
            store.set('state', fresh);
            return fresh;
        }
        st.usedWords = Array.isArray(st.usedWords) ? st.usedWords : [];
        st.pool = Array.isArray(st.pool) ? st.pool : [];
        st.actsKeys = Array.isArray(st.actsKeys) ? st.actsKeys : [];
        st.logs = Array.isArray(st.logs) ? st.logs : [];
        return st;
    }
    const saveState = (st) => store.set('state', st);

    function log(msg) {
        const st = loadState();
        const line = new Date().toLocaleTimeString('zh-CN', { hour12: false }) + '  ' + msg;
        st.logs.push(line);
        if (st.logs.length > 80) st.logs = st.logs.slice(-80);
        saveState(st);
        renderLog();
        try { console.log('[Rewards]', msg); } catch (e) { /* ignore */ }
    }

    function setStatus(msg) {
        const st = loadState();
        st.status = msg;
        saveState(st);
    }

    /* ============================================================
     * 6. 多标签页锁（防止多个标签同时刷）
     * ============================================================ */
    function tabId() {
        let id = sessionStorage.getItem('brh_tab');
        if (!id) { id = 't' + Math.random().toString(36).slice(2, 9); sessionStorage.setItem('brh_tab', id); }
        return id;
    }
    function acquireLock() {
        const l = store.get('lock', null);
        const now = Date.now();
        if (l && l.id !== tabId() && now - (l.ts || 0) < 120000) return false;
        store.set('lock', { id: tabId(), ts: now });
        return true;
    }
    function isOwnerTab() {
        const l = store.get('lock', null);
        if (!l) return true;
        return l.id === tabId() || Date.now() - (l.ts || 0) > 120000;
    }
    function releaseLock() {
        const l = store.get('lock', null);
        if (l && l.id === tabId()) store.del('lock');
    }

    /* ============================================================
     * 7. 词库池
     * ============================================================ */
    async function ensurePool(need) {
        const st = loadState();
        let pool = st.pool.filter((w) => !st.usedWords.includes(w));
        if (pool.length >= need) return pool;

        const bag = [];
        if (cfg.useHot) {
            bag.push(...await fetchHotWords());
            const seeds = uniq(bag.concat(LOCAL_BASE)).slice(0, 24);
            const picked = [];
            for (let i = 0; i < 3; i++) {
                const s = pick(seeds).split(' ')[0];
                if (picked.includes(s)) continue;
                picked.push(s);
                const sug = await bingSuggest(s);
                if (sug.length) bag.push(...sug.slice(rand(0, 4), rand(5, 10)));
            }
        }
        bag.push(...LOCAL_BASE);

        // 词形修饰，避免永远搜一模一样的串
        const decorated = [];
        bag.forEach((w) => {
            const t = String(w).replace(/\s+/g, ' ').trim();
            if (!t) return;
            decorated.push(t);
            if (Math.random() < 0.4) {
                const tail = pick(TAILS.filter(Boolean));
                if (!t.endsWith(tail.trim())) decorated.push(t + tail);
            }
        });

        pool = uniq(decorated).filter((w) => w.length > 1 && w.length < 45 && !st.usedWords.includes(w));
        if (pool.length < need) pool = pool.concat(genLocalWords(need - pool.length + 6, st.usedWords.concat(pool)));

        // 洗牌，打散来源
        for (let i = pool.length - 1; i > 0; i--) {
            const j = rand(0, i);
            [pool[i], pool[j]] = [pool[j], pool[i]];
        }
        st.pool = pool;
        saveState(st);
        log(`词库已补充：可用 ${pool.length} 条`);
        return pool;
    }

    async function pickWord() {
        const st = loadState();
        const need = clamp(cfg.target - st.count + 4, 1, 14);
        const pool = await ensurePool(need);
        const w = pool[0] || pick(LOCAL_BASE);
        const st2 = loadState();
        st2.pool = st2.pool.filter((x) => x !== w);
        saveState(st2);
        return w;
    }

    /* ============================================================
     * 8. 页面判定 / 积分 / 风控
     * ============================================================ */
    const isRewards = () => location.hostname === 'rewards.bing.com';
    const isSearchPage = () => /\/search\b/.test(location.pathname) || /br_msg=Please-Wait/.test(location.search);
    const isBingHome = () => !isRewards() && location.pathname === '/';

    function readPoints() {
        const sels = ['#id_rc', '.points-container .points', 'mee-rewards-points-balance',
            '.b_points', '[data-role="points"]', '.rewards-points'];
        for (const s of sels) {
            const el = $(s);
            if (!el) continue;
            const m = (el.textContent || '').replace(/\s/g, '').match(/(\d[\d,]*)/);
            if (m) return parseInt(m[1].replace(/,/g, ''), 10);
        }
        return null;
    }

    function updatePoints(countMiss) {
        const p = readPoints();
        if (p == null) return;
        const st = loadState();
        const previous = st.pointsNow;
        if (st.pointsBase == null) st.pointsBase = p;
        st.pointsNow = p;
        if (previous != null && p > previous) st.noPoint = 0;
        else if (countMiss && previous != null) st.noPoint = (st.noPoint || 0) + 1;
        saveState(st);
        return st.noPoint;
    }

    const HOLD_TEXT = {
        needs_login: '微软登录已失效或尚未登录',
        verification_required: '微软要求完成验证',
        earning_limited: '微软提示计分受限',
        account_restricted: '微软提示账号异常或受限'
    };

    function classifyMessage(text) {
        if (/verify your identity|prove you.re not a robot|verify you are human|unusual traffic|验证码|安全验证|请验证.{0,6}(?:身份|帐户|账户|账号)/i.test(text)) return 'verification_required';
        if (/account (?:has been|is) (?:temporarily )?(?:locked|suspended|restricted)|账号(?:已|暂时|被)*(?:锁定|封禁|暂停使用|受限)/i.test(text)) return 'account_restricted';
        if (/limit on (?:earning|how many points)|(?:earning|search) (?:Microsoft Rewards )?points (?:is|has been) (?:temporarily )?(?:limited|restricted)|积分获取.{0,12}限制|搜索计分.{0,12}限制|赚取积分.{0,12}限制/i.test(text)) return 'earning_limited';
        if (/session (?:has )?expired|sign in to (?:Microsoft )?Rewards|登录(?:会话)?(?:已)?过期|请登录.{0,12}Rewards/i.test(text)) return 'needs_login';
        return '';
    }

    function detectRisk() {
        const t = document.title || '';
        if (/captcha/i.test(t) || /\/challenge|\/verify|br_msg=Please-Wait/i.test(location.pathname + location.search)) return 'verification_required';
        const notices = $$('[role="alert"], [aria-live="assertive"]').filter(el => !el.closest('#brh-panel'))
            .map(el => el.innerText || '').join(' ').slice(0, 1500);
        const body = isRewards() ? (document.body?.innerText || '').slice(0, 1000) : '';
        return classifyMessage(t + ' ' + notices + ' ' + body);
    }

    function checkMicrosoftSession() {
        const visible = detectRisk();
        if (visible) return Promise.resolve(visible);
        return new Promise(resolve => {
            const timer = setTimeout(() => finish('unavailable'), 7000);
            const finish = result => { clearTimeout(timer); resolve(result); };
            const check = response => {
                const final = new URL(response.finalUrl || REWARDS_URL);
                const body = response.responseText || '';
                const message = classifyMessage(body.slice(0, 2000));
                if (message) return finish(message);
                if (/\/(?:challenge|verify)\b/i.test(final.pathname)) return finish('verification_required');
                if (response.status === 401 || final.hostname === 'login.live.com'
                    || final.hostname === 'login.microsoftonline.com') return finish('needs_login');
                try {
                    const info = JSON.parse(body);
                    const user = info.dashboard?.userStatus;
                    if (!info.isError && response.status === 200 && user?.isRewardsUser !== false
                        && Number.isInteger(user?.availablePoints)) return finish('');
                    if (user?.isRewardsUser === false) return finish('needs_login');
                } catch { /* An unexpected response is not a reason to start. */ }
                finish('unavailable');
            };
            try {
                const url = 'https://rewards.bing.com/api/getuserinfo';
                if (typeof GM_xmlhttpRequest === 'function') {
                    GM_xmlhttpRequest({ method: 'GET', url, timeout: 6500, onload: check,
                        onerror: () => finish('unavailable'), ontimeout: () => finish('unavailable'),
                        headers: { Accept: 'application/json' } });
                } else if (isRewards()) {
                    fetch(url).then(async response => check({
                        status: response.status, finalUrl: response.url, responseText: await response.text()
                    })).catch(() => finish('unavailable'));
                } else finish('unavailable');
            } catch { finish('unavailable'); }
        });
    }

    async function holdRun(reason) {
        if (!HOLD_TEXT[reason]) return;
        const previous = store.get('hold', '');
        store.set('hold', reason);
        pauseRun(HOLD_TEXT[reason] + '，已暂停');
        const result = await panelRequest('hold', { reason });
        if (!result?.active && previous !== reason) notify('Rewards 助手已暂停', HOLD_TEXT[reason]);
    }

    async function checkStart() {
        const state = await panelRequest('status');
        const held = store.get('hold', '');
        if (state?.hold || (held && !state?.active)) {
            setStatus('已暂停，请先在微软官网确认账号状态，再恢复脚本');
            render();
            return false;
        }
        const reason = await checkMicrosoftSession();
        if (reason && reason !== 'unavailable') {
            await holdRun(reason);
            return false;
        }
        if (reason) {
            setStatus('暂时无法确认微软登录状态，请打开 Rewards 检查');
            render();
            return false;
        }
        if (held) store.del('hold');
        const current = loadState();
        current.sessionCheckedAt = Date.now();
        saveState(current);
        return true;
    }

    /* ============================================================
     * 9. 拟人行为
     * ============================================================ */
    async function humanBrowse() {
        const dwell = rand(cfg.dwellMin, cfg.dwellMax) * 1000;
        const t0 = Date.now();
        const maxY = Math.max(document.body ? document.body.scrollHeight : 0, window.innerHeight) * 0.9;
        const steps = rand(2, 4);
        for (let i = 0; i < steps; i++) {
            window.scrollTo({ top: rand(100, Math.max(200, maxY)), behavior: 'smooth' });
            await sleep(rand(700, 1900));
            if (Math.random() < 0.35) {
                window.scrollTo({ top: Math.max(0, window.scrollY - rand(90, 320)), behavior: 'smooth' });
                await sleep(rand(400, 1100));
            }
            if (Date.now() - t0 > dwell) break;
        }
        // 随机把鼠标“停”在一条结果上
        if (Math.random() < 0.6) {
            const links = $$('#b_results h2 a, #b_results .b_algo a');
            if (links.length) {
                const el = links[rand(0, Math.min(links.length - 1, 6))];
                el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                el.dispatchEvent(new MouseEvent('mousemove', { bubbles: true }));
            }
        }
        const rest = dwell - (Date.now() - t0);
        if (rest > 0) await sleep(rest);
    }

    /* ============================================================
     * 10. 任务节奏
     * ============================================================ */
    function computeGapMs(count) {
        const mean = (cfg.gapMin + cfg.gapMax) / 2;
        const sd = Math.max(1, (cfg.gapMax - cfg.gapMin) / 4);
        let sec = gauss(mean, sd);
        if (count < cfg.warmup) sec *= 1.6;                                   // 热身
        if (cfg.fatigue) sec *= 1 + 0.02 * count;                             // 疲劳
        const h = new Date().getHours();
        if (cfg.nightSlow && (h >= 23 || h < 7)) sec *= 1.5;                  // 深夜放慢
        if (Math.random() < 0.07) sec += rand(60, 180);                       // 随机分心
        return clamp(sec, cfg.gapMin * 0.8, cfg.gapMax * 4) * 1000;
    }

    function searchUrl(q) {
        return `https://www.bing.com/search?q=${encodeURIComponent(q)}&form=${rndStr(4)}&cvid=${hex32()}`;
    }

    function go(url) {
        const next = new URL(url);
        if (next.origin !== location.origin) next.hash = 'br-tab=' + tabId();
        location.href = next.href;
    }

    async function waitCountdown(target) {
        while (Date.now() < target) {
            const st = loadState();
            if (!st.running || st.paused) return false;
            const risk = detectRisk();
            if (risk) { await holdRun(risk); return false; }
            await sleep(clamp(target - Date.now(), 150, 1000));
        }
        const st = loadState();
        return st.running && !st.paused;
    }

    let cycleRunning = false;

    async function pageCycle() {
        if (cycleRunning) return;
        const st0 = loadState();
        if (!st0.running || st0.paused) { render(); return; }
        if (!isOwnerTab()) { setStatus('另一标签页正在运行'); render(); return; }
        cycleRunning = true;
        try {
            const risk = detectRisk();
            if (risk) { await holdRun(risk); return; }
            if (Date.now() - (st0.sessionCheckedAt || 0) >= 60000 && !await checkStart()) {
                if (!loadState().paused) pauseRun('暂时无法确认微软登录状态，已暂停');
                return;
            }

            await humanBrowse();

            const miss = true;
            const noPoint = updatePoints(miss);
            if (noPoint != null && noPoint >= cfg.tolerance) {
                pauseRun(`连续 ${noPoint} 次未涨积分，已暂停`);
                notify('Rewards 助手已暂停', '连续多次未涨积分，请到微软官网检查账号状态');
                return;
            }

            let st = loadState();
            if (!st.running || st.paused) { render(); return; }

            if (st.count >= cfg.target) { await finishSearch(); return; }

            const word = await pickWord();
            let waitMs = computeGapMs(st.count);
            let extra = 0;
            if (st.count > 0 && st.count % cfg.groupSize === 0) {
                const restMs = rand(cfg.restMin, cfg.restMax) * 60000;
                waitMs += restMs;
                extra = restMs;
                log(`第 ${st.count} 次完成，进入组间休息 ${Math.round(restMs / 60000)} 分钟`);
            }

            st = loadState();
            if (!st.running || st.paused || !isOwnerTab()) return;
            st.count += 1;
            st.currentWord = word;
            st.nextAt = Date.now() + waitMs;
            st.usedWords.push(word);
            if (st.usedWords.length > 500) st.usedWords = st.usedWords.slice(-500);
            st.status = extra
                ? `组间休息中（第 ${st.count}/${cfg.target} 次）`
                : `第 ${st.count}/${cfg.target} 次：${word}`;
            saveState(st);
            render();

            const ok = await waitCountdown(st.nextAt);
            if (!ok) {
                // 等待期间被暂停/停止：回滚这一次计数，避免恢复后重复计次
                const rb = loadState();
                rb.count = Math.max(0, rb.count - 1);
                const idx = rb.usedWords.lastIndexOf(word);
                if (idx >= 0) rb.usedWords.splice(idx, 1);
                if (!rb.pool.includes(word)) rb.pool.unshift(word);
                saveState(rb);
                render();
                return;
            }
            const riskBeforeSearch = detectRisk();
            if (riskBeforeSearch) { await holdRun(riskBeforeSearch); return; }
            if (isOwnerTab()) go(searchUrl(word));
        } catch (e) {
            log('流程异常：' + (e && e.message ? e.message : e));
        } finally {
            cycleRunning = false;
        }
    }

    async function startRun(automatic = false, ticket = null) {
        const initial = loadState();
        const recovering = automatic && initial.paused && !!store.get('hold', '');
        if ((initial.running && !recovering) || initial.phase === 'activity'
            || (automatic && ((initial.paused && !recovering) || initial.phase === 'done' || initial.count >= cfg.target))) return;
        if (!await checkStart()) return;
        const st = loadState();
        if ((st.running && !recovering) || st.phase === 'activity' || !acquireLock()) return;
        if (automatic && !(await panelRequest('claim', { ticket }))?.started) {
            releaseLock();
            return;
        }
        st.running = true;
        st.paused = false;
        st.phase = 'search';
        st.nextAt = 0;
        st.lastStart = Date.now();
        saveState(st);
        heartbeatStart();
        log('开始任务');
        render();

        if (isSearchPage()) { pageCycle(); return; }

        const word = await pickWord();
        const st2 = loadState();
        if (!st2.running || st2.paused || st2.date !== st.date || !isOwnerTab()) return;
        const reason = detectRisk();
        if (reason) { await holdRun(reason); return; }
        st2.count += 1;
        st2.currentWord = word;
        st2.usedWords.push(word);
        st2.nextAt = Date.now() + rand(3, 8) * 1000;
        st2.status = `第 ${st2.count}/${cfg.target} 次：${word}`;
        saveState(st2);
        go(searchUrl(word));
    }

    function pauseRun(reason) {
        const st = loadState();
        st.paused = true;
        st.status = reason || '已暂停';
        saveState(st);
        log('暂停：' + (reason || '手动'));
        render();
    }

    async function resumeRun() {
        if (!await checkStart() || !acquireLock()) return;
        const st = loadState();
        st.paused = false;
        st.running = true;
        st.nextAt = Date.now() + rand(4, 10) * 1000;
        st.status = '已恢复';
        saveState(st);
        heartbeatStart();
        log('恢复运行');
        render();
        if (st.phase === 'activity') {
            st.running = false;
            saveState(st);
            releaseLock();
            if (isRewards()) await onRewardsPage();
            else openRewards();
        } else await pageCycle();
    }

    async function startActivities() {
        const st = loadState();
        if (st.running || st.phase === 'activity' || !await checkStart()) return;
        const current = loadState();
        if (current.running || current.phase === 'activity') return;
        current.phase = 'activity';
        current.paused = false;
        saveState(current);
        openRewards();
    }

    function stopRun() {
        const st = loadState();
        st.running = false;
        st.paused = false;
        st.nextAt = 0;
        st.status = '已停止';
        saveState(st);
        releaseLock();
        log('已停止');
        panelRequest('finish', { status: 'stopped', reason: '用户在 Rewards 助手中停止任务' });
        render();
    }

    async function finishSearch() {
        const st = loadState();
        st.running = false;
        st.paused = false;
        st.phase = cfg.autoActivity ? 'activity' : 'done';
        st.nextAt = 0;
        st.status = `搜索任务完成（${st.count}/${cfg.target}）`;
        saveState(st);
        releaseLock();
        log(`今日搜索完成，共 ${st.count} 次`);
        panelRequest('finish', {
            status: 'success',
            points: st.pointsNow != null && st.pointsBase != null ? st.pointsNow - st.pointsBase : null,
            reason: st.status,
        });
        render();
        if (cfg.notify) notify('Rewards 搜索完成', `今日已完成 ${st.count} 次搜索`);
        if (cfg.autoActivity) {
            await sleep(1500);
            openRewards();
        }
    }

    function openRewards() {
        const url = REWARDS_URL + '?br=1&t=' + Date.now();
        try {
            if (typeof GM_openInTab === 'function') {
                GM_openInTab(url, { active: true, insert: true, setParent: true });
                return;
            }
            const w = window.open(url, '_blank');
            if (!w) location.href = url;
        } catch (e) { location.href = url; }
    }

    /* ============================================================
     * 11. 每日活动
     * ============================================================ */
    const ACT_SELS = [
        '#daily-sets mee-card', '#daily-sets .mee-card', '#daily-sets .c-card',
        'mee-rewards-daily-set-item', '#more-activities mee-card', '.daily-sets .c-card',
        '[class*="daily-set"] [class*="card"]'
    ];

    function activityCards() {
        let cards = [];
        ACT_SELS.forEach((s) => { try { cards.push(...$$(s)); } catch (e) { } });
        cards = uniq(cards);
        if (!cards.length) {
            cards = uniq($$('a[href]').map((a) => a.closest('li,div[class*="card"],section')).filter(Boolean));
        }
        return cards;
    }

    function cardDone(card) {
        const txt = ((card.getAttribute('aria-label') || '') + ' ' + (card.className || '') + ' ' + (card.innerText || '')).toLowerCase();
        return /complete|completed|已完成|已领取|done/.test(txt);
    }

    function cardLink(card) {
        const a = card.querySelector('a[href]');
        if (!a || !/^https?:/i.test(a.href)) return null;
        let host = '';
        try { host = new URL(a.href).hostname; } catch (e) { return null; }
        // 只认 bing 站内的任务链接，避免误开推广 / 外站
        if (!/(^|\.)bing\.com$/i.test(host)) return null;
        if (/rewards\.bing\.com\/(dashboard|refer|about|earn|redeem|creategoal|pointsbreakdown|help)/i.test(a.href)) return null;
        return a.href.split('#')[0];
    }

    async function waitFor(fn, timeout, step) {
        const t0 = Date.now();
        while (Date.now() - t0 < timeout) {
            const v = fn();
            if (v) return v;
            await sleep(step || 600);
        }
        return null;
    }

    async function doActivities() {
        let st = loadState();
        if (st.actsDate !== todayStr()) { st.actsDate = todayStr(); st.actsKeys = []; saveState(st); }
        setStatus('正在扫描每日活动…');
        render();

        const cards = await waitFor(() => { const c = activityCards(); return c.length ? c : null; }, 15000, 800);
        if (!cards || !cards.length) {
            setStatus('未找到活动卡片（可能已全部完成）');
            render();
            return 0;
        }

        const todo = [];
        cards.forEach((c) => {
            if (cardDone(c)) return;
            const href = cardLink(c);
            if (!href) return;
            if (todo.some((x) => x.href === href)) return;
            if (st.actsKeys.includes(href)) return;
            todo.push({ href, text: (c.innerText || '').replace(/\s+/g, ' ').slice(0, 30) });
        });

        const list = todo.slice(0, cfg.maxActivities);
        log(`发现 ${todo.length} 个待做活动，本次打开 ${list.length} 个`);

        for (let i = 0; i < list.length; i++) {
            if (loadState().paused || !isOwnerTab()) return i;
            const reason = detectRisk();
            if (reason) { await holdRun(reason); return i; }
            const item = list[i];
            setStatus(`打开活动 ${i + 1}/${list.length}：${item.text}`);
            render();
            try {
                const url = item.href + (item.href.includes('#') ? '' : '#br_act=1');
                if (typeof GM_openInTab === 'function') {
                    GM_openInTab(url, { active: false, insert: true, setParent: true });
                } else {
                    window.open(url, '_blank');
                }
            } catch (e) { /* ignore */ }
            st = loadState();
            st.actsKeys.push(item.href);
            saveState(st);
            await sleep(rand(3500, 8000));
        }

        setStatus(`已打开 ${list.length} 个活动`);
        render();
        if (list.length && cfg.notify) notify('Rewards', `已打开 ${list.length} 个每日活动，答题类需手动完成`);
        return list.length;
    }

    /* ============================================================
     * 12. 控制台 UI
     * ============================================================ */
    const CSS = `
#brh-panel{position:fixed;right:18px;bottom:18px;width:298px;z-index:2147483000;
 font:12px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#e9edf3;
 background:rgba(22,25,31,.94);backdrop-filter:blur(16px) saturate(1.2);
 border:1px solid rgba(255,255,255,.09);border-radius:14px;
 box-shadow:0 14px 40px rgba(0,0,0,.5);overflow:hidden;user-select:none}
#brh-panel *{box-sizing:border-box}
#brh-panel.fold .brh-body{display:none}
#brh-panel .brh-head{display:flex;align-items:center;gap:7px;padding:9px 11px;cursor:move;
 background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.02))}
#brh-panel .brh-title{font-weight:600;font-size:12px;letter-spacing:.3px}
#brh-panel .brh-dot{width:7px;height:7px;border-radius:50%;background:#6b7280;flex:0 0 auto}
#brh-panel.run .brh-dot{background:#3bd47f;box-shadow:0 0 8px #3bd47f}
#brh-panel.pause .brh-dot{background:#f5b13d;box-shadow:0 0 8px #f5b13d}
#brh-panel.err .brh-dot{background:#f2555a;box-shadow:0 0 8px #f2555a}
#brh-panel .brh-sp{flex:1}
#brh-panel .brh-ico{width:22px;height:22px;border:0;border-radius:6px;background:rgba(255,255,255,.07);
 color:#cbd5e1;cursor:pointer;font-size:13px;line-height:1;display:flex;align-items:center;justify-content:center}
#brh-panel .brh-ico:hover{background:rgba(255,255,255,.16);color:#fff}
#brh-panel .brh-body{padding:10px 11px 11px}
#brh-panel .brh-row{display:flex;align-items:center;gap:6px;font-size:11px;color:#9aa4b2;margin-bottom:6px}
#brh-panel .brh-row b{color:#e9edf3;font-weight:600;font-size:12px}
#brh-panel .brh-bar{height:4px;border-radius:4px;background:rgba(255,255,255,.09);overflow:hidden;margin-bottom:9px}
#brh-panel .brh-bar i{display:block;height:100%;width:0;border-radius:4px;
 background:linear-gradient(90deg,#4f9cf9,#3bd47f);transition:width .4s}
#brh-panel .brh-status{font-size:11.5px;color:#c7d0dc;margin-bottom:4px;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#brh-panel .brh-timer{font-size:22px;font-weight:700;letter-spacing:.5px;
 font-variant-numeric:tabular-nums;color:#4f9cf9;line-height:1.2;margin-bottom:2px}
#brh-panel .brh-word{font-size:11px;color:#7f8a99;min-height:16px;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis;margin-bottom:9px}
#brh-panel .brh-btns{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:8px}
#brh-panel button.brh-btn{height:28px;border:1px solid rgba(255,255,255,.1);border-radius:7px;
 background:rgba(255,255,255,.06);color:#dfe6ee;font-size:11.5px;cursor:pointer;transition:.15s}
#brh-panel button.brh-btn:hover{background:rgba(255,255,255,.15)}
#brh-panel button.brh-btn.pri{background:linear-gradient(180deg,#3f8ef2,#2f78d8);border-color:#4f9cf9;color:#fff}
#brh-panel button.brh-btn.pri:hover{filter:brightness(1.1)}
#brh-panel .brh-cfg{display:none;border-top:1px solid rgba(255,255,255,.08);padding-top:8px;margin-bottom:6px}
#brh-panel .brh-cfg.on{display:block}
#brh-panel .brh-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
#brh-panel .brh-field{display:flex;flex-direction:column;gap:2px}
#brh-panel .brh-field span{font-size:10px;color:#8b95a3}
#brh-panel .brh-field input{width:100%;height:24px;border-radius:6px;border:1px solid rgba(255,255,255,.12);
 background:rgba(0,0,0,.3);color:#e9edf3;font-size:11px;padding:0 6px;outline:none}
#brh-panel .brh-field input:focus{border-color:#4f9cf9}
#brh-panel .brh-opts{display:flex;flex-wrap:wrap;gap:8px;margin-top:7px;font-size:10.5px;color:#9aa4b2}
#brh-panel .brh-opts label{display:flex;align-items:center;gap:3px;cursor:pointer}
#brh-panel .brh-save{margin-top:8px;width:100%;height:26px;border-radius:7px;border:1px solid #4f9cf9;
 background:rgba(79,156,249,.15);color:#8fc0ff;cursor:pointer;font-size:11.5px}
#brh-panel .brh-save:hover{background:rgba(79,156,249,.28)}
#brh-panel .brh-log{border-top:1px solid rgba(255,255,255,.08);padding-top:7px;max-height:88px;
 overflow:auto;font-size:10.5px;color:#6f7a88;line-height:1.7;font-family:ui-monospace,Consolas,monospace}
#brh-panel .brh-log div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#brh-panel .brh-log::-webkit-scrollbar{width:4px}
#brh-panel .brh-log::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:4px}
`;

    function injectStyle() {
        try {
            if (typeof GM_addStyle === 'function') { GM_addStyle(CSS); return; }
        } catch (e) { /* ignore */ }
        const s = document.createElement('style');
        s.textContent = CSS;
        (document.head || document.documentElement).appendChild(s);
    }

    let panel = null;

    function injectPanel() {
        if (!cfg.showPanel) return;
        if ($('#brh-panel')) { panel = $('#brh-panel'); return; }
        injectStyle();
        const el = document.createElement('div');
        el.id = 'brh-panel';
        el.innerHTML = `
<div class="brh-head">
  <span class="brh-dot"></span>
  <span class="brh-title">Rewards 助手</span>
  <span class="brh-sp"></span>
  <button class="brh-ico" data-act="cfg" title="设置">⚙</button>
  <button class="brh-ico" data-act="fold" title="折叠">—</button>
</div>
<div class="brh-body">
  <div class="brh-row"><span>今日</span><b data-k="count">0/30</b>
    <span style="margin-left:auto">积分</span><b data-k="pts">+0</b></div>
  <div class="brh-bar"><i data-k="fill"></i></div>
  <div class="brh-status" data-k="status">待机中</div>
  <div class="brh-timer" data-k="timer">--</div>
  <div class="brh-word" data-k="word"></div>
  <div class="brh-btns">
    <button class="brh-btn pri" data-act="start">开始</button>
    <button class="brh-btn" data-act="pause">暂停</button>
    <button class="brh-btn" data-act="act">每日活动</button>
  </div>
  <div class="brh-cfg">
    <div class="brh-grid">
      <label class="brh-field"><span>目标次数</span><input data-c="target" type="number" min="1" max="200"></label>
      <label class="brh-field"><span>每组次数</span><input data-c="groupSize" type="number" min="1" max="50"></label>
      <label class="brh-field"><span>间隔下限(秒)</span><input data-c="gapMin" type="number" min="3" max="600"></label>
      <label class="brh-field"><span>间隔上限(秒)</span><input data-c="gapMax" type="number" min="5" max="900"></label>
      <label class="brh-field"><span>休息下限(分)</span><input data-c="restMin" type="number" min="0" max="120"></label>
      <label class="brh-field"><span>休息上限(分)</span><input data-c="restMax" type="number" min="0" max="180"></label>
      <label class="brh-field"><span>停留下限(秒)</span><input data-c="dwellMin" type="number" min="2" max="120"></label>
      <label class="brh-field"><span>停留上限(秒)</span><input data-c="dwellMax" type="number" min="3" max="180"></label>
    </div>
    <div class="brh-opts">
      <label><input type="checkbox" data-o="useHot">热搜词</label>
      <label><input type="checkbox" data-o="autoActivity">自动活动</label>
      <label><input type="checkbox" data-o="nightSlow">夜间减速</label>
      <label><input type="checkbox" data-o="fatigue">疲劳模拟</label>
    </div>
    <button class="brh-save" data-act="save">保存设置</button>
  </div>
  <div class="brh-log"></div>
</div>`;
        document.body.appendChild(el);
        panel = el;

        // 拖动
        const head = $('.brh-head', el);
        let drag = null;
        head.addEventListener('mousedown', (e) => {
            if (e.target.closest('button')) return;
            const r = el.getBoundingClientRect();
            drag = { dx: e.clientX - r.left, dy: e.clientY - r.top, w: r.width };
            e.preventDefault();
        });
        document.addEventListener('mousemove', (e) => {
            if (!drag) return;
            el.style.left = clamp(e.clientX - drag.dx, 0, window.innerWidth - drag.w) + 'px';
            el.style.top = clamp(e.clientY - drag.dy, 0, window.innerHeight - 60) + 'px';
            el.style.right = 'auto';
            el.style.bottom = 'auto';
        });
        document.addEventListener('mouseup', () => {
            if (!drag) return;
            drag = null;
            store.set('ui_pos', { left: el.style.left, top: el.style.top });
        });
        const pos = store.get('ui_pos', null);
        if (pos && pos.left) { el.style.left = pos.left; el.style.top = pos.top; el.style.right = 'auto'; el.style.bottom = 'auto'; }

        el.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-act]');
            if (!btn) return;
            const act = btn.dataset.act;
            if (act === 'fold') { el.classList.toggle('fold'); btn.textContent = el.classList.contains('fold') ? '+' : '—'; }
            else if (act === 'cfg') { $('.brh-cfg', el).classList.toggle('on'); syncCfgUI(); }
            else if (act === 'start') { if (loadState().paused) resumeRun(); else startRun(); }
            else if (act === 'pause') { const st = loadState(); if (st.paused) resumeRun(); else pauseRun('手动暂停'); }
            else if (act === 'act') { startActivities(); }
            else if (act === 'save') { saveCfgFromUI(); }
        });

        syncCfgUI();
        render();
    }

    function syncCfgUI() {
        if (!panel) return;
        $$('[data-c]', panel).forEach((i) => { i.value = cfg[i.dataset.c]; });
        $$('[data-o]', panel).forEach((i) => { i.checked = !!cfg[i.dataset.o]; });
    }

    function saveCfgFromUI() {
        if (!panel) return;
        const num = (k, min, max) => {
            const v = parseInt($(`[data-c="${k}"]`, panel).value, 10);
            cfg[k] = isNaN(v) ? DEFAULT_CFG[k] : clamp(v, min, max);
        };
        num('target', 1, 200);
        num('groupSize', 1, 50);
        num('gapMin', 3, 600);
        num('gapMax', 5, 900);
        num('restMin', 0, 120);
        num('restMax', 0, 180);
        num('dwellMin', 2, 120);
        num('dwellMax', 3, 180);
        if (cfg.gapMax < cfg.gapMin) cfg.gapMax = cfg.gapMin;
        if (cfg.restMax < cfg.restMin) cfg.restMax = cfg.restMin;
        if (cfg.dwellMax < cfg.dwellMin) cfg.dwellMax = cfg.dwellMin;
        $$('[data-o]', panel).forEach((i) => { cfg[i.dataset.o] = i.checked; });
        saveCfg();
        syncCfgUI();
        log('设置已保存');
        render();
    }

    function renderLog() {
        if (!panel) return;
        const box = $('.brh-log', panel);
        if (!box) return;
        const st = loadState();
        const lines = st.logs.slice(-6).reverse();
        box.innerHTML = lines.map((l) => `<div>${l.replace(/</g, '&lt;')}</div>`).join('');
    }

    function render() {
        if (!panel) return;
        const st = loadState();
        panel.classList.remove('run', 'pause', 'err');
        if (st.running && st.paused) panel.classList.add('pause');
        else if (st.running) panel.classList.add('run');
        if (/受限|验证|暂停/.test(st.status || '')) panel.classList.add('err');

        const cnt = $('[data-k="count"]', panel);
        if (cnt) cnt.textContent = `${st.count}/${cfg.target}`;

        const pts = $('[data-k="pts"]', panel);
        if (pts) {
            const d = (st.pointsNow != null && st.pointsBase != null) ? st.pointsNow - st.pointsBase : null;
            pts.textContent = d == null ? '—' : (d >= 0 ? '+' : '') + d;
        }

        const fill = $('[data-k="fill"]', panel);
        if (fill) fill.style.width = Math.min(100, (st.count / cfg.target) * 100) + '%';

        const stEl = $('[data-k="status"]', panel);
        if (stEl) stEl.textContent = st.status || '待机中';

        const tm = $('[data-k="timer"]', panel);
        if (tm) {
            if (st.running && !st.paused && st.nextAt > Date.now()) tm.textContent = fmtDur(st.nextAt - Date.now());
            else if (st.running && st.paused) tm.textContent = '已暂停';
            else if (!st.running && st.count >= cfg.target) tm.textContent = '已完成';
            else tm.textContent = '--';
        }

        const wd = $('[data-k="word"]', panel);
        if (wd) wd.textContent = st.currentWord ? '当前：' + st.currentWord : '';

        const startBtn = $('[data-act="start"]', panel);
        if (startBtn) startBtn.textContent = st.paused ? '继续' : '开始';
        const pauseBtn = $('[data-act="pause"]', panel);
        if (pauseBtn) pauseBtn.textContent = st.paused ? '继续' : '暂停';
    }

    /* ============================================================
     * 13. Worker 计时（后台标签页不被节流）
     * ============================================================ */
    let tickerStarted = false;
    function startTicker(cb) {
        if (tickerStarted) return;
        tickerStarted = true;
        try {
            const code = "let t=null;onmessage=e=>{if(e.data==='start'){clearInterval(t);t=setInterval(()=>postMessage(1),1000)}}";
            const url = URL.createObjectURL(new Blob([code], { type: 'text/javascript' }));
            const w = new Worker(url);
            w.onmessage = cb;
            w.postMessage('start');
            setInterval(() => {
                if (loadState().running && isOwnerTab()) store.set('lock', { id: tabId(), ts: Date.now() });
            }, 30000);
        } catch (e) {
            setInterval(cb, 1000);
        }
    }

    function heartbeatStart() {
        const st = loadState();
        if (st.running && isOwnerTab()) acquireLock();
    }

    /* ============================================================
     * 14. 通知 / 菜单
     * ============================================================ */
    function notify(title, text) {
        try {
            if (typeof GM_notification === 'function') {
                GM_notification({ title, text, timeout: 8000 });
                return;
            }
            if ('Notification' in window && Notification.permission === 'granted') new Notification(title, { body: text });
        } catch (e) { /* ignore */ }
    }

    function clearToday() {
        store.del('state');
        store.del('lock');
        log('已清除今日状态');
        render();
        alert('已清除今日状态，可重新开始');
    }

    function registerMenu() {
        if (typeof GM_registerMenuCommand !== 'function') return;
        const reg = (n, f) => { try { GM_registerMenuCommand(n, f); } catch (e) { /* ignore */ } };
        reg('▶ 开始 / 继续任务', () => { if (loadState().paused) resumeRun(); else startRun(); });
        reg('⏸ 暂停任务', () => pauseRun('手动暂停'));
        reg('⏹ 停止任务', () => stopRun());
        reg('🎯 立即做每日活动', startActivities);
        reg('⚙ 打开设置面板', () => {
            if (!cfg.showPanel) { cfg.showPanel = true; saveCfg(); injectPanel(); }
            if (panel) { panel.classList.remove('fold'); $('.brh-cfg', panel).classList.add('on'); syncCfgUI(); }
        });
        reg('📊 打开 Rewards 面板', () => openRewards());
        reg('🗑 清除今日状态', () => clearToday());
        reg('🔑 设置热搜 API Key', () => {
            const k = prompt('gmya.net API Key（可留空，留空也能用免费热搜 + Bing 联想词）', cfg.apiKey || '');
            if (k !== null) { cfg.apiKey = k.trim(); saveCfg(); loadState(); log('API Key 已更新'); }
        });
    }

    /* ============================================================
     * 15. 页面入口
     * ============================================================ */
    async function onRewardsPage() {
        const st = loadState();
        if (st.paused || !isOwnerTab()) return;
        if (st.phase === 'activity') {
            if (!await checkStart() || !acquireLock()) return;
            const n = await doActivities();
            const st2 = loadState();
            releaseLock();
            if (st2.paused) return;
            st2.phase = 'done';
            st2.status = n ? `已打开 ${n} 个活动` : '每日活动已处理';
            saveState(st2);
            panelRequest('finish', {
                status: 'success',
                points: st2.pointsNow != null && st2.pointsBase != null ? st2.pointsNow - st2.pointsBase : null,
                reason: st2.status,
            });
            render();
            if (cfg.notify) notify('Rewards 每日任务', n ? `活动已打开 ${n} 个` : '今日活动已完成');
        } else if (!st.running) {
            setStatus(st.count >= cfg.target ? `今日已完成 ${st.count} 次搜索` : '待机中');
            render();
        }
    }

    async function main() {
        // 脚本自己打开的活动标签页：只静默挂载，不跑主流程
        if (/#br_act=1/.test(location.hash)) return;

        const continuedTab = location.hash.match(/^#br-tab=(t[a-z0-9]{7})$/)?.[1];
        if (continuedTab && store.get('lock', null)?.id === continuedTab) {
            sessionStorage.setItem('brh_tab', continuedTab);
            history.replaceState(history.state, '', location.pathname + location.search);
        }

        injectPanel();
        render();
        panelRequest('heartbeat', { version: SCRIPT_VERSION, page: location.hostname });
        if (PANEL_BRIDGE) setInterval(() => panelRequest('heartbeat', {
            version: SCRIPT_VERSION, page: location.hostname,
        }), 60000);

        const st = loadState();
        if (st.running) heartbeatStart();   // 续上多标签锁

        startTicker(() => render());
        registerMenu();

        const ticket = location.hash.match(/^#rp-start=([A-Za-z0-9_-]{24})$/)?.[1] || null;
        if (ticket) history.replaceState(history.state, '', location.pathname + location.search);
        if ((isBingHome() || (isRewards() && location.pathname === '/dashboard')) && PANEL_BRIDGE) {
            const remote = await panelRequest('status');
            if (remote?.active && (ticket ? remote.scheduled : remote.openPage)) await startRun(true, ticket);
        }

        if (isRewards()) { await onRewardsPage(); return; }
        if (loadState().paused) return;
        if (isSearchPage()) {
            await pageCycle();
            return;
        }

        // Bing 首页
        if (st.running && !st.paused) {
            await sleep(rand(1200, 2500));
            pageCycle();
        } else {
            render();
        }
    }

    function boot() {
        main().catch((e) => {
            try { console.error('[Rewards] 初始化失败', e); } catch (_) { }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot, { once: true });
    } else {
        boot();
    }
})();
