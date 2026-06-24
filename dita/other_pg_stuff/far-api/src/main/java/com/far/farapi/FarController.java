package com.far.farapi;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
public class FarController {

    private final FarRag rag;
    private final FarRetriever retriever;

    public FarController(FarRag rag, FarRetriever retriever) {
        this.rag = rag;
        this.retriever = retriever;
    }

    /** Ask Claude a FAR question:  GET /ask?q=When must an agency publicize a synopsis? */
    @GetMapping("/ask")
    public String ask(@RequestParam String q) {
        return rag.ask(q);
    }

    /** Raw keyword hits (no LLM):  GET /search?q=synopsis&k=5 */
    @GetMapping("/search")
    public List<FarRetriever.Hit> search(@RequestParam String q,
                                         @RequestParam(defaultValue = "5") int k) {
        return retriever.keywordSearch(q, k);
    }

    /** Graph neighbours of an item:  GET /refs?id=FAR_5_203_g */
    @GetMapping("/refs")
    public List<FarRetriever.Ref> refs(@RequestParam String id) {
        return retriever.references(id);
    }
}
