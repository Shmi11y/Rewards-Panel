// Observe the engine's original account events before its console filters and masking.
// Only known status codes, account identifiers and point totals leave this adapter.
const fs = require('node:fs');
const path = require('node:path');
const cluster = require('node:cluster');

const LOGIN_REQUIRED = /没有可用的(?:非交互|受支持)登录方式|未配置密码且无法返回登录方式选择|需要(?:手动|人工|重新)登录|登录(?:态|会话)(?:已)?(?:过期|失效)|Login failed or aborted at state: (?:PASSWORD_INPUT|RECOVERY_EMAIL_INPUT|EMAIL_VERIFICATION_INPUT|SIGN_IN_METHOD_PICKER|LOGIN_PASSWORDLESS|2FA_TOTP|OTP_CODE_ENTRY)|Microsoft login error:|Passwordless authentication timeout|Recovery email is required/i;

function observe(eventsDir, accounts) {
    if (!eventsDir) return;
    const eventsPath = path.join(eventsDir, `${process.pid}.jsonl`);
    function emit(event) {
        try {
            if (event.type === 'login') event.at = Date.now();
            fs.appendFileSync(eventsPath, JSON.stringify(event) + '\n', 'utf8');
        } catch {
            // Recording must not interrupt the task engine.
        }
    }

    if (cluster.isPrimary) emit({type: 'accounts', emails: accounts.map(account => account.email)});
    const loggerPath = path.join(__dirname, 'app', 'dist', 'logging', 'Logger.js');
    if (!fs.existsSync(loggerPath)) return;
    const { Logger } = require(loggerPath);
    if (typeof Logger?.prototype?.baseLog !== 'function') return;

    const baseLog = Logger.prototype.baseLog;
    const active = new WeakMap();
    Logger.prototype.baseLog = function (level, platform, tag, message, color) {
        const text = typeof message === 'string' ? message : '';
        if (tag === 'ACCOUNT-START') {
            const email = /^开始处理账户:\s*([^\s|]+)/.exec(text)?.[1];
            if (email) {
                active.set(this, email);
                emit({type: 'started', email});
            }
        } else if (tag === 'ACCOUNT-END' || tag === 'ACCOUNT-SKIP' || tag === 'ACCOUNT-ERROR' || tag === 'ACCOUNT-PARTIAL') {
            const email = /^(?:(?:账户完成|跳过账户):\s*|账户中断但已获得部分积分\s*\|\s*)?([^\s|]+@[^\s|]+)/.exec(text)?.[1];
            if (email) {
                const points = /\| 获得积分=(-?\d+)/.exec(text);
                const type = {'ACCOUNT-END': 'success', 'ACCOUNT-SKIP': 'skipped', 'ACCOUNT-ERROR': 'failed', 'ACCOUNT-PARTIAL': 'partial'}[tag];
                emit({type, email, points: points ? Number(points[1]) : type === 'skipped' ? 0 : null});
                if (type === 'failed' && LOGIN_REQUIRED.test(text)) emit({type: 'login', email});
            }
        } else if (tag === 'FLOW' && level === 'error') {
            const email = /^([^\s@]+@[^\s@]+) 的移动端流程失败:/.exec(text)?.[1];
            if (email) {
                emit({type: 'failed', email});
                if (LOGIN_REQUIRED.test(text)) emit({type: 'login', email});
            }
        } else if (tag === 'LOGIN' && text === '登录完成，会话已保存') {
            const email = active.get(this);
            if (email) emit({type: 'login_ok', email});
        } else if ((tag === 'LOGIN' || tag === 'LOGIN-PASSWORDLESS' || tag === 'LOGIN-RECOVERY') && LOGIN_REQUIRED.test(text)) {
            const email = active.get(this);
            if (email) emit({type: 'login', email});
        } else if (cluster.isPrimary && (tag === 'RUN-END' || tag === 'RUN-SKIP')) {
            emit({type: tag === 'RUN-SKIP' ? 'run_skipped' : 'run_ended', failed: text.includes('存在失败')});
        }
        return baseLog.call(this, level, platform, tag, message, color);
    };
}

module.exports = { observe };
