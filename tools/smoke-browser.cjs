const path = require('node:path');
const { chromium } = require(path.join(process.cwd(), 'app/node_modules/patchright'));

async function test() {
    for (const headless of [true]) {
        const browser = await chromium.launch({ headless });
        try {
            const page = await browser.newPage();
            await page.goto('data:text/html,<title>Rewards Panel smoke test</title>');
            if (await page.title() !== 'Rewards Panel smoke test') {
                throw new Error('Chromium did not load the test page');
            }
        } finally {
            await browser.close();
        }
    }
}

test().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
