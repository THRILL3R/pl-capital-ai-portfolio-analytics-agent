import { Link, Navigate, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { About } from './pages/About';
import { Ask } from './pages/Ask';
import { Evaluation } from './pages/Evaluation';
import { Exposure } from './pages/Exposure';
import { Portfolios } from './pages/Portfolios';

function NotFound() {
  return (
    <div className="card mx-auto max-w-xl p-8 text-center">
      <p className="eyebrow mb-2">Page not found</p>
      <h1 className="text-headline-lg text-indigo-800">That page does not exist</h1>
      <Link to="/ask" className="mt-4 inline-block rounded-full bg-primary-container px-5 py-2 text-label-lg text-on-primary hover:bg-primary-hover">
        Go to Ask
      </Link>
    </div>
  );
}

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/ask" replace />} />
        <Route path="ask" element={<Ask />} />
        <Route path="portfolios" element={<Portfolios />} />
        <Route path="exposure" element={<Exposure />} />
        <Route path="evaluation" element={<Evaluation />} />
        <Route path="about" element={<About />} />
        <Route path="how-it-works" element={<Navigate to="/about#how-it-works" replace />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
