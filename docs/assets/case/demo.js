"use strict";
(async () => {
  const status = document.querySelector("#status");
  try {
    const response = await fetch("assets/case/data.json");
    if (!response.ok) throw new Error("fixture unavailable");
    const data = await response.json();
    if (data.synthetic !== true || data.seed !== 20261027)
      throw new Error("wrong fixture");
    const rows = data.rows.filter((r) => [8, 9].includes(r.month));
    const money = (value) =>
      new Intl.NumberFormat("ru-RU").format(value) + " ₽";
    const sign = (value) => (value > 0 ? "+" : "") + money(value);
    const totals = [8, 9].map((month) =>
      rows
        .filter((r) => r.month === month && r.operation === "ПРИХОД")
        .reduce((s, r) => s + r.amount, 0),
    );
    if (totals[0] !== 850 || totals[1] !== 1410)
      throw new Error("fixture totals mismatch");
    status.textContent =
      "Синтетическая fixture загружена · 850 + 100 + 420 + 70 − 30 = 1 410 ₽";
    const components = [
      ["База", 850],
      ["Цена", 100],
      ["Количество", 420],
      ["Новые товары", 70],
      ["Ушедшие", -30],
      ["Несопоставимые", 0],
      ["Сверка", 0],
      ["Округление", 0],
      ["Текущий итог", 1410],
    ];
    const bridge = document.querySelector("#bridge");
    components.forEach(([label, amount], i) => {
      const line = document.createElement("div");
      line.className = "bar-row";
      const name = document.createElement("span");
      name.textContent = label;
      const track = document.createElement("div");
      const bar = document.createElement("div");
      bar.className =
        "bar " +
        ([0, 8].includes(i)
          ? "total"
          : amount < 0
            ? "down"
            : amount === 0
              ? "zero"
              : "");
      bar.style.width = `${(Math.abs(amount) / 1410) * 100}%`;
      track.append(bar);
      const number = document.createElement("strong");
      number.textContent = [0, 8].includes(i) ? money(amount) : sign(amount);
      line.append(name, track, number);
      bridge.append(line);
    });
    const show = (product) => {
      const milk = product === "demo-milk-1l";
      document
        .querySelector("#milk-button")
        .setAttribute("aria-pressed", String(milk));
      document
        .querySelector("#rice-button")
        .setAttribute("aria-pressed", String(!milk));
      document.querySelector("#product-summary").textContent = milk
        ? "Молоко: 8 → 12 л; 50 → 65 ₽/л. Δ +380 ₽ = цена +150 ₽ + количество +230 ₽."
        : "Рис: 8 → 12 кг; 50 → 45 ₽/кг. Δ +140 ₽ = цена −50 ₽ + количество +190 ₽.";
      const body = document.querySelector("#observations");
      body.replaceChildren();
      rows
        .filter((r) => r.product === product)
        .sort((a, b) => a.date.localeCompare(b.date))
        .forEach((row) => {
          const tr = document.createElement("tr");
          [
            row.id,
            row.date,
            row.store,
            `${row.quantity} ${milk ? "л" : "кг"}`,
            money(row.price),
            money(row.amount),
          ].forEach((value) => {
            const td = document.createElement("td");
            td.textContent = value;
            tr.append(td);
          });
          body.append(tr);
        });
    };
    document
      .querySelector("#milk-button")
      .addEventListener("click", () => show("demo-milk-1l"));
    document
      .querySelector("#rice-button")
      .addEventListener("click", () => show("demo-rice-1kg"));
    const factor = (quantity) => {
      document
        .querySelector("#price-button")
        .setAttribute("aria-pressed", String(!quantity));
      document
        .querySelector("#quantity-button")
        .setAttribute("aria-pressed", String(quantity));
      document.querySelector("#factor-explanation").textContent = quantity
        ? "Количество +420 ₽ = молоко +230 ₽ + рис +190 ₽. Обоих товаров купили по 12 вместо 8 единиц. Цена берётся как средняя между месяцами."
        : "Цена +100 ₽ = молоко +150 ₽ + рис −50 ₽. Формула: изменение цены × среднее количество 10. Рост расходов не равен росту цен.";
    };
    document
      .querySelector("#price-button")
      .addEventListener("click", () => factor(false));
    document
      .querySelector("#quantity-button")
      .addEventListener("click", () => factor(true));
    document.querySelector("#july-button").addEventListener("click", () => {
      const july = document.querySelector("#july");
      july.hidden = !july.hidden;
      document
        .querySelector("#july-button")
        .setAttribute("aria-expanded", String(!july.hidden));
    });
    show("demo-milk-1l");
  } catch (error) {
    status.textContent =
      "Не удалось проверить fixture. Запустите страницу через HTTP по инструкции README.";
    document.querySelectorAll("button").forEach((button) => {
      button.disabled = true;
    });
  }
})();
