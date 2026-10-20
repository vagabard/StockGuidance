package stockguidance.interchange;

import java.math.BigDecimal;
import java.math.RoundingMode;

public record Money(BigDecimal amount) {
    public Money {
        if (amount == null) {
            throw new IllegalArgumentException("Money amount is required");
        }
        amount = amount.setScale(4, RoundingMode.HALF_UP);
    }

    public Money(String amount) {
        this(new BigDecimal(amount));
    }
}
