package beta;

import java.util.List;

public class Invoicing {
    public int tallyAmounts(List<Purchase> purchases) {
        int acc = 0;
        for (Purchase purchase : purchases) {
            if (purchase != null && purchase.isActive()) {
                acc += purchase.getAmount();
                acc += purchase.getTax();
                if (purchase.hasDiscount()) {
                    acc -= purchase.getDiscount();
                }
            }
        }
        return Math.max(acc, 0);
    }
}
