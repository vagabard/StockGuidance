package stockguidance.migration;

import jdk.incubator.vector.DoubleVector;
import jdk.incubator.vector.VectorOperators;
import jdk.incubator.vector.VectorSpecies;

final class ZScoreVectorKernel {
    private static final VectorSpecies<Double> SPECIES = DoubleVector.SPECIES_PREFERRED;

    private ZScoreVectorKernel() {
    }

    static double[] scores(double[][] values, int first, int end) {
        int count = values[0].length;
        double[] result = new double[count];
        int window = end - first;
        for (int offset = 0; offset < count; offset += SPECIES.length()) {
            var mask = SPECIES.indexInRange(offset, count);
            DoubleVector anchor = DoubleVector.fromArray(SPECIES, values[first], offset, mask);
            DoubleVector total = DoubleVector.zero(SPECIES);
            DoubleVector correction = DoubleVector.zero(SPECIES);
            for (int session = first; session < end; session++) {
                DoubleVector current = DoubleVector.fromArray(SPECIES, values[session], offset, mask).sub(anchor);
                DoubleVector partial = total.add(current);
                correction = correction.add(current.sub(partial).add(total).blend(
                        total.sub(partial).add(current), total.abs().compare(VectorOperators.GE, current.abs())));
                total = partial;
            }
            DoubleVector deltaSum = total.add(correction);
            DoubleVector mean = anchor.add(deltaSum.div(window));
            total = DoubleVector.zero(SPECIES);
            correction = DoubleVector.zero(SPECIES);
            for (int session = first; session < end; session++) {
                DoubleVector centered = DoubleVector.fromArray(SPECIES, values[session], offset, mask).sub(anchor);
                DoubleVector current = centered.mul(centered);
                DoubleVector partial = total.add(current);
                correction = correction.add(current.sub(partial).add(total).blend(
                        total.sub(partial).add(current), total.abs().compare(VectorOperators.GE, current.abs())));
                total = partial;
            }
            DoubleVector variance = total.add(correction).sub(deltaSum.mul(deltaSum).div(window)).div(window);
            DoubleVector current = DoubleVector.fromArray(SPECIES, values[end - 1], offset, mask);
            current.sub(mean).div(variance.sqrt()).intoArray(result, offset, mask);
        }
        return result;
    }
}
