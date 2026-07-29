import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, HashRouter } from 'react-router'

import { App } from './App'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      refetchOnWindowFocus: false,
    },
  },
})

const application = (
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>
)
const useHashRouter = import.meta.env.VITE_ROUTER_MODE === 'hash'

createRoot(document.getElementById('root')!).render(
  useHashRouter
    ? <HashRouter>{application}</HashRouter>
    : (
        <BrowserRouter
          basename={import.meta.env.BASE_URL.replace(/\/$/, '') || '/'}
        >
          {application}
        </BrowserRouter>
      ),
)
