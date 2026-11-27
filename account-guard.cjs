const fs = require('node:fs');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');

const SCORING_WARNING = 'Fraud_UserWarning_BotScore_UX';
const VERIFY_STATES = new Set(['EMAIL_VERIFICATION_INPUT', 'RECOVERY_EMAIL_INPUT',
    '2FA_TOTP', 'OTP_CODE_ENTRY', 'LOGIN_PASSWORDLESS', 'PASSWORDLESS_SEND_CODE']);
const LOGIN_HOSTS = new Set(['login.live.com', 'account.live.com', 'account.microsoft.com', 'login.microsoftonline.com']);
const NOTICE_PAGE_HOSTS = new Set([...LOGIN_HOSTS, 'rewards.bing.com', 'rewards.microsoft.com']);
const NOTICE_API_HOSTS = new Set([...NOTICE_PAGE_HOSTS, 'www.bing.com', 'prod.rewardsplatform.microsoft.com']);
const labels = {
    earning_limited: /\b(?:(?:there is|you have) a limit on (?:earning|how many points you can earn)|you can only earn points for searches after|earning (?:Microsoft Rewards )?points (?:is|has been) (?:temporarily )?(?:limited|restricted)|(?:Microsoft Rewards|search) (?:earning|earnings|points) (?:limit|restriction))\b|(?:积分获取|赚取积分|搜索计分)(?:已|暂时|受到|被)*限制|(?:每隔|每过).{0,20}(?:才能|可再次)赚取(?:搜索)?积分/i,
    verification_required: /\b(?:verify your identity|help us verify (?:your identity|it'?s you)|prove you'?re not a robot|we need to verify (?:your account|your identity|it'?s you))\b|验证你的身份|验证您的身份|请验证(?:你的|您的)?(?:身份|帐户|账户|账号)|证明你不是机器人/i,
    account_restricted: /\b(?:(?:your|this) (?:Microsoft(?: Rewards)? )?account (?:has been|is) (?:temporarily )?(?:locked|suspended|restricted)|unusual activity (?:on|in) your (?:Microsoft )?account)\b|(?:帐户|账户|账号)(?:已|暂时|被)*(?:锁定|封禁|暂停使用|受限)|(?:你的|您的)(?:帐户|账户|账号)(?:存在|出现)异常活动/i,
};

function reasonForText(text) {
    if (typeof text !== 'string') return null;
    return Object.keys(labels).find(reason => labels[reason].test(text.slice(0, 4000))) ?? null;
}

function folder(dataDir) {
    return path.join(dataDir, 'account-holds');
}

function holdPath(dataDir, email) {
    const key = createHash('sha256').update(email.trim().toLowerCase()).digest('hex');
    return path.join(folder(dataDir), key + '.json');
}

function isHeld(dataDir, email) {
    try {
        fs.statSync(holdPath(dataDir, email));
        return true;
    } catch (error) {
        if (error.code === 'ENOENT') return false;
        throw error;
    }
}

function persist(dataDir, email, reason, runId) {
    const target = holdPath(dataDir, email);
    const temporary = target + '.' + randomUUID() + '.tmp';
    fs.mkdirSync(folder(dataDir), { recursive: true });
    try {
        fs.writeFileSync(temporary, JSON.stringify({email, reason, runId, at: Date.now()}), {flag: 'wx'});
        fs.renameSync(temporary, target);
    } finally {
        fs.rmSync(temporary, {force: true});
    }
}

function microsoftHost(url) {
    try {
        const parsed = new URL(url);
        if (parsed.protocol === 'https:') return parsed.hostname;
    } catch {
        // Internal browser pages do not represent Microsoft notices.
    }
    return '';
}

function responseReason(url, data) {
    if (!NOTICE_API_HOSTS.has(microsoftHost(url)) || !data || typeof data !== 'object') return null;
    if ([data.dashboard?.userWarnings, data.flyoutResult?.userWarnings, data.userWarnings]
        .some(warnings => Array.isArray(warnings) && warnings.some(warning => warning?.name === SCORING_WARNING))) {
        return 'earning_limited';
    }
    return [data.errorMessage, data.error_description, data.error?.message, data.userInfo?.errorMessage]
        .map(reasonForText).find(Boolean) ?? null;
}

function installGuards(dataDir, runId, emit) {
    const { Login } = require('./app/dist/browser/auth/Login.js');
    const { default: Browser } = require('./app/dist/browser/Browser.js');
    const { default: Http } = require('./app/dist/util/Http.js');
    const { default: Util } = require('./app/dist/util/Utils.js');
    const { Impit } = require('./app/node_modules/impit');
    const contexts = new Map();
    const holds = new Map();
    const controllers = new Map();
    const entry = require.resolve('./app/dist/index.js');
    const currentEmail = () => require.cache[entry]?.exports.getCurrentContext?.()?.account?.email;
    const key = email => email.trim().toLowerCase();

    function check(email) {
        if (email && (holds.has(key(email)) || isHeld(dataDir, email))) {
            throw new Error('Rewards account execution is paused');
        }
    }

    function signalFor(email) {
        const id = key(email);
        if (!controllers.has(id)) controllers.set(id, new AbortController());
        return controllers.get(id).signal;
    }

    function untilHeld(email, operation) {
        if (!email) return operation();
        check(email);
        const signal = signalFor(email);
        let abort;
        const stopped = new Promise((_, reject) => {
            abort = () => reject(signal.reason);
            signal.addEventListener('abort', abort, {once: true});
        });
        return Promise.race([stopped, Promise.resolve().then(operation)])
            .finally(() => signal.removeEventListener('abort', abort));
    }

    function stop(email, reason) {
        const id = key(email);
        if (!holds.has(id)) {
            holds.set(id, reason);
            try {
                persist(dataDir, email, reason, runId);
                emit({type: 'restricted', email, reason});
                console.warn('[ACCOUNT-HOLD] 检测到微软明确提示，本账号任务已暂停。');
            } finally {
                controllers.get(id)?.abort(new Error('Rewards account execution is paused'));
                for (const context of contexts.get(id) ?? []) {
                    void (context.browser()?.close() ?? context.close()).catch(() => {});
                }
            }
        }
        check(email);
    }

    async function checkPage(page, email) {
        if (!NOTICE_PAGE_HOSTS.has(microsoftHost(page.url()))) return;
        const notices = await page.evaluate(() => Array.from(document.querySelectorAll('h1,h2,[role="alert"]'))
            .filter(node => node.getClientRects().length > 0)
            .slice(0, 16)
            .map(node => String(node.innerText ?? '').slice(0, 1000))).catch(() => []);
        const reason = notices.map(reasonForText).find(Boolean);
        if (reason) stop(email, reason);
    }

    const detectState = Login.prototype.detectCurrentState;
    Login.prototype.detectCurrentState = async function (page) {
        const email = currentEmail();
        check(email);
        if (email) await checkPage(page, email);
        const state = await detectState.call(this, page);
        if (email && LOGIN_HOSTS.has(microsoftHost(page.url()))) {
            if (state === 'ACCOUNT_LOCKED') stop(email, 'account_restricted');
            if (VERIFY_STATES.has(state)) stop(email, 'verification_required');
        }
        check(email);
        return state;
    };

    const createBrowser = Browser.prototype.createBrowser;
    Browser.prototype.createBrowser = async function (account) {
        const {email} = account;
        check(email);
        const result = await createBrowser.call(this, account);
        const {context} = result;
        const id = key(email);
        if (holds.has(id) || isHeld(dataDir, email)) {
            await (context.browser()?.close() ?? context.close()).catch(() => {});
            check(email);
        }
        if (!contexts.has(id)) contexts.set(id, new Set());
        contexts.get(id).add(context);
        context.once('close', () => {
            const remaining = contexts.get(id);
            remaining?.delete(context);
            if (!remaining?.size) contexts.delete(id);
        });
        context.on('page', page => {
            page.on('domcontentloaded', () => {
                void checkPage(page, email).catch(() => {});
            });
        });
        return result;
    };

    const fetch = Impit.prototype.fetch;
    Impit.prototype.fetch = function (url, options) {
        const email = currentEmail();
        if (!email) return fetch.call(this, url, options);
        check(email);
        const signal = signalFor(email);
        const combined = options?.signal ? AbortSignal.any([options.signal, signal]) : signal;
        return fetch.call(this, url, {...options, signal: combined});
    };

    const request = Http.prototype.request;
    Http.prototype.request = async function (config, ...args) {
        const email = currentEmail();
        check(email);
        let response;
        try {
            response = await untilHeld(email, () => request.call(this, config, ...args));
        } catch (error) {
            const reason = responseReason(config.url, error?.response?.data);
            if (email && reason) stop(email, reason);
            throw error;
        }
        const reason = responseReason(config.url, response?.data);
        if (email && reason) stop(email, reason);
        check(email);
        return response;
    };

    const wait = Util.prototype.wait;
    Util.prototype.wait = async function (...args) {
        const email = currentEmail();
        check(email);
        await untilHeld(email, () => wait.apply(this, args));
        check(email);
    };
}

module.exports = { installGuards, isHeld, reasonForText, responseReason };
