import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import "katex/dist/katex.min.css";

try {
  document.documentElement.dataset.theme =
    window.localStorage.getItem("ai-systems-performance-lab-theme") === "dark"
      ? "dark"
      : "light";
} catch {
  document.documentElement.dataset.theme = "light";
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
