import React, { createContext, useContext } from "react";

export const MARKET_META = {
  us: {
    id: "us",
    label: "US",
    currency: "USD",
    defaultTicker: "AAPL",
    sessionName: "US market",
    closeName: "US close",
    sectorLabels: {
      XLK: "Information Technology",
      XLY: "Consumer Discretionary",
      XLP: "Consumer Staples",
      XLV: "Health Care",
      XLF: "Financials",
      XLE: "Energy",
      XLI: "Industrials",
      XLB: "Materials",
      XLRE: "Real Estate",
      XLU: "Utilities",
      XLC: "Communication Services",
    },
  },
  hk: {
    id: "hk",
    label: "Hong Kong",
    currency: "HKD",
    defaultTicker: "0700.HK",
    sessionName: "Hong Kong market",
    closeName: "Hong Kong close",
    sectorLabels: {
      FINANCIALS: "Financials",
      INFOTECH: "Information Technology",
      PROPERTIES: "Properties",
      CONSUMER: "Consumer",
      HEALTHCARE: "Healthcare",
      ENERGY: "Energy",
      TELECOM: "Telecom",
      UTILITIES: "Utilities",
      INDUSTRIALS: "Industrials",
      TRANSPORT: "Transport",
      CONGLOMERATES: "Conglomerates",
      OTHER: "Other",
    },
  },
};

const MarketContext = createContext(MARKET_META.us);

export function MarketProvider({ market, children }) {
  const value = MARKET_META[market] || MARKET_META.us;
  return <MarketContext.Provider value={value}>{children}</MarketContext.Provider>;
}

export function useMarket() {
  return useContext(MarketContext);
}

export function withMarketQuery(path, marketId) {
  const joiner = path.includes("?") ? "&" : "?";
  return `${path}${joiner}market=${encodeURIComponent(marketId)}`;
}
