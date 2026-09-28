package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Functions of the agentauth chaincode the bridge will forward. Anything else
// is refused before it reaches Drunix.
var submitFunctions = map[string]bool{
	"RegisterMandate": true, "RevokeMandate": true, "RegisterRootCapability": true, "Delegate": true,
	"Reserve": true, "Commit": true, "Release": true, "ReturnUnused": true, "Revoke": true,
}

var evaluateFunctions = map[string]bool{
	"Info": true, "GetMandate": true, "GetCapability": true, "GetReservation": true, "GetAuthorityChain": true,
}

const maxBody = 64 << 10

type callRequest struct {
	Function string   `json:"function"`
	Args     []string `json:"args"`
}

type apiError struct {
	Category       string `json:"category"`
	Code           string `json:"code,omitempty"`
	Stage          string `json:"stage,omitempty"`
	Message        string `json:"message"`
	TransactionID  string `json:"tx_id,omitempty"`
	BlockNumber    uint64 `json:"block_number,omitempty"`
	ValidationCode string `json:"validation_code,omitempty"`
}

type submitResponse struct {
	OK             bool            `json:"ok"`
	Function       string          `json:"function"`
	TransactionID  string          `json:"tx_id"`
	BlockNumber    uint64          `json:"block_number"`
	Status         string          `json:"status"`
	ValidationCode string          `json:"validation_code"`
	Result         json.RawMessage `json:"result"`
	Channel        string          `json:"channel"`
	Chaincode      string          `json:"chaincode"`
	LatencyMs      int64           `json:"latency_ms"`
	EndorseMs      int64           `json:"endorse_ms"`
	CommitMs       int64           `json:"commit_ms"`
}

type errorResponse struct {
	OK       bool     `json:"ok"`
	Function string   `json:"function,omitempty"`
	Error    apiError `json:"error"`
}

// TxRecord is an entry in the bridge's recent-transaction log.
type TxRecord struct {
	Time          time.Time `json:"time"`
	Function      string    `json:"function"`
	TransactionID string    `json:"tx_id,omitempty"`
	BlockNumber   uint64    `json:"block_number,omitempty"`
	Status        string    `json:"status"`
	Category      string    `json:"category,omitempty"`
	Code          string    `json:"code,omitempty"`
	LatencyMs     int64     `json:"latency_ms"`
}

// Server is the bridge's HTTP API.
type Server struct {
	ledger    Ledger
	channel   string
	chaincode string
	peer      string
	msp       string
	token     string
	log       *slog.Logger

	mu     sync.Mutex
	recent []TxRecord
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.health)
	mux.HandleFunc("POST /v1/submit", s.auth(s.submit))
	mux.HandleFunc("POST /v1/evaluate", s.auth(s.evaluate))
	mux.HandleFunc("GET /v1/transactions", s.auth(s.transactions))
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.token != "" {
			got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if subtle.ConstantTimeCompare([]byte(got), []byte(s.token)) != 1 {
				writeJSON(w, http.StatusUnauthorized, errorResponse{Error: apiError{Category: CatUnauthorized, Message: "missing or invalid bridge token"}})
				return
			}
		}
		next(w, r)
	}
}

func decode(r *http.Request, allowed map[string]bool) (*callRequest, *apiError) {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil {
		return nil, &apiError{Category: CatBadRequest, Message: "cannot read request body"}
	}
	if len(body) > maxBody {
		return nil, &apiError{Category: CatBadRequest, Message: "request body too large"}
	}
	var req callRequest
	dec := json.NewDecoder(strings.NewReader(string(body)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		return nil, &apiError{Category: CatBadRequest, Message: "malformed JSON: " + err.Error()}
	}
	if req.Function == "" {
		return nil, &apiError{Category: CatBadRequest, Message: "function is required"}
	}
	if !allowed[req.Function] {
		return nil, &apiError{Category: CatBadRequest, Message: "function " + req.Function + " is not allowed on this endpoint"}
	}
	if req.Args == nil {
		req.Args = []string{}
	}
	return &req, nil
}

func statusFor(category string) int {
	switch category {
	case CatChaincodeRejected, CatMVCCConflict:
		return http.StatusConflict
	case CatInvalidCommit:
		return http.StatusBadGateway
	case CatTimeout:
		return http.StatusGatewayTimeout
	case CatUnavailable:
		return http.StatusServiceUnavailable
	case CatBadRequest:
		return http.StatusBadRequest
	default:
		return http.StatusInternalServerError
	}
}

func toAPIError(err error) apiError {
	var le *LedgerError
	if !errors.As(err, &le) {
		le = classify(StageSubmit, "", err)
	}
	return apiError{Category: le.Category, Code: le.Code, Stage: le.Stage, Message: le.Message,
		TransactionID: le.TransactionID, BlockNumber: le.BlockNumber, ValidationCode: le.ValidationCode}
}

func rawResult(b []byte) json.RawMessage {
	if len(b) == 0 {
		return json.RawMessage("null")
	}
	if json.Valid(b) {
		return json.RawMessage(b)
	}
	q, _ := json.Marshal(string(b))
	return json.RawMessage(q)
}

func (s *Server) record(rec TxRecord) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.recent = append(s.recent, rec)
	if len(s.recent) > 200 {
		s.recent = s.recent[len(s.recent)-200:]
	}
}

func (s *Server) submit(w http.ResponseWriter, r *http.Request) {
	req, bad := decode(r, submitFunctions)
	if bad != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: *bad})
		return
	}
	t0 := time.Now()
	res, err := s.ledger.Submit(r.Context(), req.Function, req.Args)
	latency := time.Since(t0).Milliseconds()
	if err != nil {
		ae := toAPIError(err)
		s.log.Warn("drunix submit rejected", "function", req.Function, "category", ae.Category, "code", ae.Code,
			"stage", ae.Stage, "tx_id", ae.TransactionID, "latency_ms", latency)
		s.record(TxRecord{Time: t0, Function: req.Function, TransactionID: ae.TransactionID, BlockNumber: ae.BlockNumber,
			Status: "REJECTED", Category: ae.Category, Code: ae.Code, LatencyMs: latency})
		writeJSON(w, statusFor(ae.Category), errorResponse{Function: req.Function, Error: ae})
		return
	}
	s.log.Info("drunix submit VALID", "function", req.Function, "tx_id", res.TransactionID, "block", res.BlockNumber,
		"latency_ms", latency)
	s.record(TxRecord{Time: t0, Function: req.Function, TransactionID: res.TransactionID, BlockNumber: res.BlockNumber,
		Status: "VALID", LatencyMs: latency})
	writeJSON(w, http.StatusOK, submitResponse{
		OK: true, Function: req.Function, TransactionID: res.TransactionID, BlockNumber: res.BlockNumber,
		Status: "VALID", ValidationCode: res.ValidationCode, Result: rawResult(res.Result),
		Channel: s.channel, Chaincode: s.chaincode, LatencyMs: latency, EndorseMs: res.EndorseMs, CommitMs: res.CommitMs,
	})
}

func (s *Server) evaluate(w http.ResponseWriter, r *http.Request) {
	req, bad := decode(r, evaluateFunctions)
	if bad != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: *bad})
		return
	}
	t0 := time.Now()
	out, err := s.ledger.Evaluate(r.Context(), req.Function, req.Args)
	if err != nil {
		ae := toAPIError(err)
		writeJSON(w, statusFor(ae.Category), errorResponse{Function: req.Function, Error: ae})
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"ok": true, "function": req.Function, "result": rawResult(out), "latency_ms": time.Since(t0).Milliseconds(),
	})
}

func (s *Server) transactions(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	out := make([]TxRecord, len(s.recent))
	copy(out, s.recent)
	s.mu.Unlock()
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "transactions": out})
}

// health reports "ok" only when the Gateway answers a real query against the
// deployed agentauth chaincode.
func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	ready, conn := s.ledger.Ready()
	body := map[string]interface{}{
		"bridge": "ok", "channel": s.channel, "chaincode": s.chaincode, "peer_endpoint": s.peer, "msp_id": s.msp,
		"gateway_connection": conn,
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	t0 := time.Now()
	out, err := s.ledger.Evaluate(ctx, "Info", nil)
	body["query_latency_ms"] = time.Since(t0).Milliseconds()
	if err != nil {
		ae := toAPIError(err)
		body["status"] = "degraded"
		body["drunix"] = "unreachable"
		body["chaincode_status"] = "unknown"
		body["error"] = ae
		if ae.Category == CatChaincodeRejected {
			body["drunix"] = "reachable"
			body["chaincode_status"] = "not_ready"
		}
		writeJSON(w, http.StatusServiceUnavailable, body)
		return
	}
	body["status"] = "ok"
	body["drunix"] = "reachable"
	body["chaincode_status"] = "ready"
	body["contract"] = rawResult(out)
	body["gateway_ready"] = ready
	writeJSON(w, http.StatusOK, body)
}
