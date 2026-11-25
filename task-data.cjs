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
        loadAccounts();
    } finally {
        process.chdir(originalDir);
    }
}
