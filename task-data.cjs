const path = require('node:path');
const { isMainThread } = require('node:worker_threads');

if (isMainThread) {
    const dataDir = path.resolve(process.env.REWARDS_DATA_DIR || path.join(__dirname, 'data'));
    const originalDir = process.cwd();
    const { loadConfig, loadAccounts } = require('./app/dist/util/Load.js');

    process.env.REWARDS_DATA_DIR = dataDir;
    try {
        // The published engine reads config.json and .env from its working directory.
        process.chdir(dataDir);
        const config = loadConfig();
        config.sessionPath = path.resolve(dataDir, config.sessionPath);
        if (process.env.REWARDS_RETRY_ACCOUNT === '1') {
            if (config.humanize) config.humanize.skipWhenCompletedToday = false;
            require('./app/dist/util/Humanize.js').markRunSucceeded = () => {};
        }
        const accounts = loadAccounts();
        const selectedAccount = process.env.REWARDS_ONLY_ACCOUNT;
        if (selectedAccount && (accounts.length !== 1 || accounts[0].email.toLowerCase() !== selectedAccount.toLowerCase())) {
            throw new Error('单账号任务的账号列表与所选账号不一致，已取消运行');
        }
        if (process.env.REWARDS_RUN_EVENTS) {
            require('./run-events.cjs').observe(process.env.REWARDS_RUN_EVENTS, accounts);
        }
    } finally {
        process.chdir(originalDir);
    }
}
