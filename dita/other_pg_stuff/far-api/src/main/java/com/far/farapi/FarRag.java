package com.far.farapi;

import org.springframework.ai.chat.client.ChatClient;
import org.springframework.stereotype.Service;

import static java.util.stream.Collectors.joining;

/** Retrieve FAR excerpts, then have Claude answer grounded only in them. */
@Service
public class FarRag {

    private final ChatClient chat;
    private final FarRetriever retriever;

    public FarRag(ChatClient.Builder builder, FarRetriever retriever) {
        this.chat = builder.build();
        this.retriever = retriever;
    }

    public String ask(String question) {
        var hits = retriever.keywordSearch(question, 6);
        if (hits.isEmpty()) {
            return "No matching FAR text found for: " + question;
        }
        var context = hits.stream()
                .map(h -> "[" + h.farAddress() + "] " + h.text())
                .collect(joining("\n\n"));

        return chat.prompt()
                .system("""
                        You answer questions about the Federal Acquisition Regulation (FAR).
                        Use ONLY the provided excerpts. Cite the bracketed far_address for each
                        claim. If the excerpts do not contain the answer, say so plainly.
                        """)
                .user(u -> u.text("Question: {q}\n\nExcerpts:\n{ctx}")
                            .param("q", question)
                            .param("ctx", context))
                .call()
                .content();
    }
}
