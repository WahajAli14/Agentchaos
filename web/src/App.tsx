import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { BenchmarkPage } from "./pages/BenchmarkPage";
import { InvariantsPage } from "./pages/InvariantsPage";
import { ProvidersPage } from "./pages/ProvidersPage";
import { SettingsPage } from "./pages/SettingsPage";
import { RunsPage } from "./pages/RunsPage";
import { ScenariosPage } from "./pages/ScenariosPage";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/scenarios" replace />} />
        <Route path="scenarios/:fault?" element={<ScenariosPage />} />
        <Route path="runs/:runId?" element={<RunsPage />} />
        <Route path="benchmark" element={<BenchmarkPage />} />
        <Route path="invariants/:name?" element={<InvariantsPage />} />
        <Route path="providers" element={<ProvidersPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/scenarios" replace />} />
      </Route>
    </Routes>
  );
}
