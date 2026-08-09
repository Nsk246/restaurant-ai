import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import Pass from "./Pass";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Pass />
  </StrictMode>,
);
