// Pass to Playwright CLI run-code --filename after opening docs/demo.html at 1280x900.
async (page) => {
  const start = Date.now();
  const until = async (ms) =>
    page.waitForTimeout(Math.max(0, ms - (Date.now() - start)));
  await until(12000);
  await page.locator("#bridge-section").scrollIntoViewIfNeeded();
  await until(24000);
  await page
    .getByRole("button", { name: "Разобрать фактор цены +100 ₽", exact: true })
    .click();
  await until(36000);
  await page.locator("#product-section").scrollIntoViewIfNeeded();
  await until(48000);
  await page
    .getByRole("button", { name: "Рис 1 кг · +140 ₽", exact: true })
    .click();
  await until(60000);
  await page.locator("#quality-section").scrollIntoViewIfNeeded();
  await until(70000);
  await page
    .getByRole("button", { name: "Проверить неполный июль", exact: true })
    .click();
  await until(75000);
};
