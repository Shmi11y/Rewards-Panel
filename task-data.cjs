const path = require('node:path');
const cluster = require('node:cluster');
const { isMainThread } = require('node:worker_threads');

if (isMainThread) {
    const dataDir = path.resolve(process.env.REWARDS_DATA_DIR || path.join(__dirname, 'data'));
    const originalDir = process.cwd();
    const loader = require('./app/dist/util/Load.js');
    const validator = require('./app/dist/util/Validator.js');
    const { installGuards, isHeld } = require('./account-guard.cjs');

    process.env.REWARDS_DATA_DIR = dataDir;
    try {
        // The published engine reads config.json and .env from its working directory.
        process.chdir(dataDir);
        const config = loader.loadConfig();
        config.contintueOnBotWarning = false;
        config.sessionPath = path.resolve(dataDir, config.sessionPath);
        if (process.env.REWARDS_RETRY_ACCOUNT === '1') {
            if (config.humanize) config.humanize.skipWhenCompletedToday = false;
            require('./app/dist/util/Humanize.js').markRunSucceeded = () => {};
        }
        const validateAccounts = validator.validateAccounts;
        let emails = [];
        const heldEmails = new Set();
        // Let the engine parse .env with its normal precedence. Filter the resulting
        // accounts before validation so an unusable paused account cannot block the rest.
        validator.validateAccounts = accounts => {
            emails = accounts.map(account => account.email);
            const selectedAccount = process.env.REWARDS_ONLY_ACCOUNT;
            const actual = emails.map(email => email.toLowerCase());
            if (selectedAccount && (actual.length !== 1 || actual[0] !== selectedAccount.toLowerCase())) {
                throw new Error('单账号任务的账号列表与所选账号不一致，已取消运行');
            }
            const expected = JSON.parse(process.env.REWARDS_SELECTED_ACCOUNTS || 'null');
            if (expected && JSON.stringify(actual) !== JSON.stringify(expected)) {
                throw new Error('任务账号列表与所选账号不一致，已取消运行');
            }
            for (const email of emails) {
                if (isHeld(dataDir, email)) heldEmails.add(email);
            }
            return validateAccounts(accounts.filter(account => !heldEmails.has(account.email)));
        };
        let eligible;
        try {
            eligible = loader.loadAccounts();
        } finally {
            validator.validateAccounts = validateAccounts;
        }
        // Override the exported loader used by the engine itself, including cluster workers.
        loader.loadAccounts = () => eligible;
        const emit = require('./run-events.cjs').observe(process.env.REWARDS_RUN_EVENTS, emails) ?? (() => {});
        if (cluster.isPrimary) {
            for (const email of heldEmails) emit({type: 'held', email});
            if (!eligible.length) emit({type: 'run_skipped'});
        }
        if (!eligible.length) process.exit(0);
        installGuards(dataDir, Number(process.env.REWARDS_RUN_ID), emit);
    } finally {
        process.chdir(originalDir);
    }
}
